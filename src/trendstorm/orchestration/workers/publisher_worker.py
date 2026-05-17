"""Publisher worker — rendering + blob-upload Kafka consumer.

Consumes `trendstorm.publish.pending.v1`. For each event:
    1. Loads Analysis and Category from Mongo.
    2. Renders MD/JSON/PDF via PublisherPipeline (PDF is best-effort).
    3. Uploads rendered files to MinIO.
    4. Persists one Report row per format.
    5. Emits stream events: STAGE_STARTED → REPORT_READY.
    6. Publishes PublishCompletedEvent to `publish.completed.v1`.

Idempotency key: `publisher:{job_id}` — one-shot per publish request.

Failure semantics:
    NotFoundError (missing analysis or category) → permanent failure.
    BlobError / transient infra issues → Kafka retry topology.
    Other unexpected exceptions → re-raise for BaseConsumer retry.

Retry topology:
    attempt 1 → RETRY_PUBLISH_30S
    attempt 2 → RETRY_PUBLISH_5M
    attempt 3 → RETRY_PUBLISH_1H
    attempt 4+ → DLQ

Run:
    python -m trendstorm.orchestration.workers.publisher_worker
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from aiokafka.errors import KafkaError
from opentelemetry import trace
from opentelemetry.propagate import inject

from trendstorm.agents.publisher.pipeline import PublisherPipeline
from trendstorm.domain.streaming.events import StreamEvent, StreamEventType
from trendstorm.infrastructure.kafka.consumer import BaseConsumer
from trendstorm.infrastructure.kafka.producer import KafkaProducerClient
from trendstorm.infrastructure.mongo.client import MongoClient
from trendstorm.infrastructure.mongo.repositories import (
    IdempotencyRepository,
    MongoAnalysisRepository,
    MongoCategoryRepository,
    MongoReportRepository,
)
from trendstorm.orchestration.events import PublishCompletedEvent, PublishPendingEvent
from trendstorm.orchestration.topics import ConsumerGroup, Topic
from trendstorm.services.streaming.emit import emit_stream_event
from trendstorm.shared.config import get_settings
from trendstorm.shared.errors import NotFoundError, TrendStormError
from trendstorm.shared.ids import new_id
from trendstorm.shared.logging import configure_logging, get_logger
from trendstorm.shared.tracing import configure_tracing, shutdown_tracing
from trendstorm.shared.tracing.semantics import Attr

if TYPE_CHECKING:
    from trendstorm.orchestration.events import EventEnvelope
    from trendstorm.shared.config import KafkaSettings

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)

_RETRY_TOPICS = [
    Topic.RETRY_PUBLISH_30S,
    Topic.RETRY_PUBLISH_5M,
    Topic.RETRY_PUBLISH_1H,
]


class PublisherWorker(BaseConsumer):
    """Consumes PublishPendingEvents and renders reports for a job."""

    def __init__(
        self,
        *,
        kafka_settings: KafkaSettings,
        pipeline: PublisherPipeline,
        idempotency: IdempotencyRepository,
        producer: KafkaProducerClient,
    ) -> None:
        super().__init__(
            topics=[Topic.PUBLISH_PENDING],
            group_id=ConsumerGroup.PUBLISHER.value,
            settings=kafka_settings,
            idempotency=idempotency,
            producer=producer,
            worker_name="publisher",
        )
        self._pipeline = pipeline

    # ------------------------------------------------------------------ #
    # Idempotency
    # ------------------------------------------------------------------ #

    def _idempotency_key(self, event: EventEnvelope) -> str | None:
        if isinstance(event, PublishPendingEvent):
            return f"publisher:{event.job_id}"
        return f"publisher:{event.event_id}"

    # ------------------------------------------------------------------ #
    # Main handler
    # ------------------------------------------------------------------ #

    async def handle(self, event: EventEnvelope) -> None:
        if not isinstance(event, PublishPendingEvent):
            logger.warning(
                "publisher_unexpected_event_type",
                event_type=getattr(event, "event_type", "unknown"),
            )
            return

        with tracer.start_as_current_span(
            "publisher.render",
            attributes={
                Attr.JOB_ID: event.job_id,
                Attr.ANALYSIS_ID: event.analysis_id,
                Attr.ATTEMPT: event.attempt,
            },
        ):
            await self._render(event)

    async def _render(self, event: PublishPendingEvent) -> None:
        """Run the full publish pipeline for one job."""
        await emit_stream_event(
            StreamEvent(
                job_id=event.job_id,
                tenant_id=event.tenant_id,
                event_type=StreamEventType.STAGE_STARTED,
                stage="publishing",
                payload={"analysis_id": event.analysis_id},
            ),
            producer=self._producer,
            correlation_id=event.correlation_id,
        )

        try:
            pipeline_result = await self._pipeline.process(
                tenant_id=event.tenant_id,
                job_id=event.job_id,
                analysis_id=event.analysis_id,
                category_id=event.category_id,
            )

            await self._publish_completed(
                event,
                success=True,
                markdown_report_id=pipeline_result.result.markdown_report_id,
                pdf_report_id=pipeline_result.result.pdf_report_id,
                json_report_id=pipeline_result.result.json_report_id,
            )

            await emit_stream_event(
                StreamEvent(
                    job_id=event.job_id,
                    tenant_id=event.tenant_id,
                    event_type=StreamEventType.REPORT_READY,
                    stage="publishing",
                    payload={
                        "analysis_id": event.analysis_id,
                        "markdown_report_id": pipeline_result.result.markdown_report_id,
                        "json_report_id": pipeline_result.result.json_report_id,
                        "pdf_report_id": pipeline_result.result.pdf_report_id,
                    },
                ),
                producer=self._producer,
                correlation_id=event.correlation_id,
            )

            logger.info(
                "publisher_render_completed",
                job_id=event.job_id,
                analysis_id=event.analysis_id,
                md_id=pipeline_result.result.markdown_report_id,
                json_id=pipeline_result.result.json_report_id,
                pdf_id=pipeline_result.result.pdf_report_id,
            )

        except NotFoundError as exc:
            # Missing analysis or category cannot be fixed by retrying.
            logger.error(
                "publisher_permanent_failure",
                job_id=event.job_id,
                analysis_id=event.analysis_id,
                error_code=exc.code,
                error=exc.message,
            )
            await self._publish_completed(
                event,
                success=False,
                error_code=exc.code,
                error_message=exc.message,
            )

    async def _publish_completed(
        self,
        event: PublishPendingEvent,
        *,
        success: bool,
        markdown_report_id: str | None = None,
        pdf_report_id: str | None = None,
        json_report_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        otel_carrier: dict[str, str] = {}
        inject(otel_carrier)

        completed = PublishCompletedEvent(
            correlation_id=event.correlation_id,
            tenant_id=event.tenant_id,
            traceparent=otel_carrier.get("traceparent"),
            job_id=event.job_id,
            success=success,
            markdown_report_id=markdown_report_id,
            pdf_report_id=pdf_report_id,
            json_report_id=json_report_id,
            error_code=error_code,
            error_message=error_message,
        )
        await self._producer.producer.send_and_wait(
            Topic.PUBLISH_COMPLETED.value,
            value=completed.model_dump_json().encode(),
            key=event.job_id.encode(),
        )

    # ------------------------------------------------------------------ #
    # Retry routing
    # ------------------------------------------------------------------ #

    async def _handle_failure(
        self, event: EventEnvelope, error: TrendStormError
    ) -> None:
        if not isinstance(event, PublishPendingEvent):
            await super()._handle_failure(event, error)
            return

        attempt = event.attempt
        retry_index = attempt - 1
        if retry_index < len(_RETRY_TOPICS):
            retry_topic = _RETRY_TOPICS[retry_index]
            retry_event = event.model_copy(
                update={"attempt": attempt + 1, "event_id": new_id()}
            )
            try:
                await self._producer.producer.send_and_wait(
                    retry_topic.value,
                    value=retry_event.model_dump_json().encode(),
                    key=event.job_id.encode(),
                )
                logger.warning(
                    "publisher_retry_scheduled",
                    job_id=event.job_id,
                    next_attempt=attempt + 1,
                    topic=retry_topic.value,
                    error_code=error.code,
                )
                return
            except KafkaError as exc:
                logger.error("retry_send_failed", error=str(exc), job_id=event.job_id)

        await self._send_to_dlq(
            event.model_dump_json().encode(),
            reason=error.code,
            detail=error.message,
        )
        logger.error(
            "publisher_sent_to_dlq",
            job_id=event.job_id,
            attempt=attempt,
            error_code=error.code,
        )


# ===========================================================================
# Process entry point
# ===========================================================================

async def run_worker() -> None:
    """Start the publisher worker process, blocking until shutdown signal."""
    settings = get_settings()
    configure_logging()
    configure_tracing(service_name="trendstorm-publisher")
    logger.info("publisher_worker_booting")

    from trendstorm.infrastructure.blob.minio_client import MinioClient

    mongo = MongoClient(settings.mongo)
    minio = MinioClient(settings.blob)
    producer = KafkaProducerClient(settings.kafka)

    await asyncio.gather(
        mongo.connect(),
        minio.connect(),
        producer.start(),
    )

    analysis_repo = MongoAnalysisRepository(mongo)
    category_repo = MongoCategoryRepository(mongo)
    report_repo = MongoReportRepository(mongo)
    idem = IdempotencyRepository(mongo)

    pipeline = PublisherPipeline(
        analysis_repo=analysis_repo,
        category_repo=category_repo,
        minio=minio,
        report_repo=report_repo,
        blob_settings=settings.blob,
    )

    worker = PublisherWorker(
        kafka_settings=settings.kafka,
        pipeline=pipeline,
        idempotency=idem,
        producer=producer,
    )
    await worker.start()
    worker.install_signal_handlers()

    try:
        await worker.run()
    finally:
        logger.info("publisher_worker_shutting_down")
        await worker.stop()
        await producer.stop()
        await minio.close()
        await mongo.close()
        shutdown_tracing()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_worker())


if __name__ == "__main__":
    main()
