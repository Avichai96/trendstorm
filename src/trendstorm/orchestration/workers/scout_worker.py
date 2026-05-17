"""Scout worker — ingestion Kafka consumer.

Consumes `trendstorm.ingest.pending.v1`, fans out N=16 concurrent fetches
(bounded by settings.ingest.concurrency_per_job), and publishes one
`trendstorm.ingest.completed.v1` event per job whether or not all sources
succeeded (partial success is acceptable).

Idempotency key: `f"scout:{event.job_id}"` — one handler call processes ALL
sources for a job. This differs from the orchestrator, which opts out entirely.

Retry topology (via `_handle_failure`):
    attempt 1 → RETRY_INGEST_30S  (attempt becomes 2)
    attempt 2 → RETRY_INGEST_5M   (attempt becomes 3)
    attempt 3 → RETRY_INGEST_1H   (attempt becomes 4)
    attempt 4+ → DLQ

Only RAISED exceptions (e.g. Mongo unreachable) trigger the retry path.
Per-source failures within the pipeline are reported in `failed_source_ids`
and do NOT trigger retries.

Run:
    python -m trendstorm.orchestration.workers.scout_worker
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

import httpx
from aiokafka.errors import KafkaError
from opentelemetry import trace
from opentelemetry.propagate import inject

from trendstorm.agents.scout.fetcher import Fetcher
from trendstorm.agents.scout.pipeline import ingest_sources
from trendstorm.agents.scout.rate_limit import RateLimiter
from trendstorm.domain.streaming.events import StreamEvent, StreamEventType
from trendstorm.infrastructure.blob.minio_client import MinioClient
from trendstorm.infrastructure.kafka.consumer import BaseConsumer
from trendstorm.infrastructure.kafka.producer import KafkaProducerClient
from trendstorm.infrastructure.mongo.client import MongoClient
from trendstorm.infrastructure.mongo.repositories import (
    IdempotencyRepository,
    MongoRawDocumentRepository,
    MongoSourceRepository,
)
from trendstorm.infrastructure.redis.client import RedisClient
from trendstorm.orchestration.events import (
    IngestCompletedEvent,
    IngestDocRef,
    IngestPendingEvent,
)
from trendstorm.orchestration.topics import ConsumerGroup, Topic
from trendstorm.services.streaming.emit import emit_stream_event
from trendstorm.shared.config import get_settings
from trendstorm.shared.ids import new_id
from trendstorm.shared.logging import configure_logging, get_logger
from trendstorm.shared.tracing import configure_tracing, shutdown_tracing
from trendstorm.shared.tracing.semantics import Attr

if TYPE_CHECKING:
    from trendstorm.orchestration.events import EventEnvelope
    from trendstorm.shared.config import IngestSettings, KafkaSettings
    from trendstorm.shared.errors import TrendStormError

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)

_RETRY_TOPICS = [
    Topic.RETRY_INGEST_30S,
    Topic.RETRY_INGEST_5M,
    Topic.RETRY_INGEST_1H,
]


class ScoutWorker(BaseConsumer):
    """Consumes IngestPendingEvents and runs the ingestion pipeline."""

    def __init__(
        self,
        *,
        kafka_settings: KafkaSettings,
        source_repo: MongoSourceRepository,
        raw_doc_repo: MongoRawDocumentRepository,
        idempotency: IdempotencyRepository,
        producer: KafkaProducerClient,
        minio: MinioClient,
        http_client: httpx.AsyncClient,
        redis: RedisClient,
        ingest_settings: IngestSettings,
    ) -> None:
        super().__init__(
            topics=[Topic.INGEST_PENDING],
            group_id=ConsumerGroup.SCOUT.value,
            settings=kafka_settings,
            idempotency=idempotency,
            producer=producer,
            worker_name="scout",
        )
        self._source_repo = source_repo
        self._raw_doc_repo = raw_doc_repo
        self._minio = minio
        self._concurrency = ingest_settings.concurrency_per_job

        rate_limiter = RateLimiter(
            redis.client,
            rate=ingest_settings.rate_limit_rate,
            burst=ingest_settings.rate_limit_burst,
        )
        self._fetcher = Fetcher(
            client=http_client,
            rate_limiter=rate_limiter,
            max_response_bytes=ingest_settings.max_response_bytes,
        )

    # ------------------------------------------------------------------ #
    # Idempotency                                                          #
    # ------------------------------------------------------------------ #

    def _idempotency_key(self, event: EventEnvelope) -> str | None:
        if isinstance(event, IngestPendingEvent):
            return f"scout:{event.job_id}"
        return f"scout:{event.event_id}"

    # ------------------------------------------------------------------ #
    # Main handler                                                         #
    # ------------------------------------------------------------------ #

    async def handle(self, event: EventEnvelope) -> None:
        """Process one IngestPendingEvent."""
        if not isinstance(event, IngestPendingEvent):
            logger.warning("scout_unexpected_event_type",
                           event_type=getattr(event, "event_type", "unknown"))
            return

        with tracer.start_as_current_span(
            "scout.ingest_job",
            attributes={
                Attr.JOB_ID: event.job_id,
                Attr.ATTEMPT: event.attempt,
                "trendstorm.source_count": len(event.source_ids),
            },
        ):
            await self._ingest(event)

    async def _ingest(self, event: IngestPendingEvent) -> None:
        # Notify SSE clients that ingestion has started.
        await emit_stream_event(
            StreamEvent(
                job_id=event.job_id,
                tenant_id=event.tenant_id,
                event_type=StreamEventType.STAGE_STARTED,
                stage="ingesting",
                payload={"source_count": len(event.source_ids)},
            ),
            producer=self._producer,
            correlation_id=event.correlation_id,
        )

        # 1. Look up all source objects (URL, type, category_id, etc.)
        sources = await self._source_repo.list_by_ids(
            event.tenant_id, event.source_ids
        )
        logger.info(
            "scout_sources_loaded",
            job_id=event.job_id,
            requested=len(event.source_ids),
            found=len(sources),
        )

        # 2. Run the bounded-concurrency pipeline
        result = await ingest_sources(
            job_id=event.job_id,
            tenant_id=event.tenant_id,
            sources=sources,
            fetcher=self._fetcher,
            raw_doc_repo=self._raw_doc_repo,
            source_repo=self._source_repo,
            minio=self._minio,
            concurrency=self._concurrency,
        )

        # 3. Convert IngestionResult → IngestCompletedEvent
        doc_refs = [
            IngestDocRef(
                id=ref.id,
                source_id=ref.source_id,
                content_hash=ref.content_hash,
                blob_uri_raw=ref.blob_uri,
                char_count=ref.char_count,
            )
            for ref in result.document_refs
        ]

        # Inject current OTel context so the orchestrator continues the trace.
        otel_carrier: dict[str, str] = {}
        inject(otel_carrier)

        completed = IngestCompletedEvent(
            correlation_id=event.correlation_id,
            tenant_id=event.tenant_id,
            traceparent=otel_carrier.get("traceparent"),
            job_id=event.job_id,
            document_refs=doc_refs,
            failed_source_ids=result.failed_source_ids,
        )

        # 4. Publish — key by job_id so the orchestrator's partition assignment is stable
        await self._producer.producer.send_and_wait(
            Topic.INGEST_COMPLETED.value,
            value=completed.model_dump_json().encode(),
            key=event.job_id.encode(),
        )

        logger.info(
            "ingest_completed_published",
            job_id=event.job_id,
            docs=len(doc_refs),
            deduped=result.deduped_count,
            failed=len(result.failed_source_ids),
        )

        # Notify SSE clients that ingestion has completed.
        await emit_stream_event(
            StreamEvent(
                job_id=event.job_id,
                tenant_id=event.tenant_id,
                event_type=StreamEventType.STAGE_COMPLETED,
                stage="ingesting",
                payload={
                    "docs_ingested": len(doc_refs),
                    "deduped": result.deduped_count,
                    "failed": len(result.failed_source_ids),
                },
            ),
            producer=self._producer,
            correlation_id=event.correlation_id,
        )

    # ------------------------------------------------------------------ #
    # Retry routing                                                        #
    # ------------------------------------------------------------------ #

    async def _handle_failure(
        self, event: EventEnvelope, error: TrendStormError
    ) -> None:
        """Route to staged retry topics before falling through to the DLQ."""
        if not isinstance(event, IngestPendingEvent):
            await super()._handle_failure(event, error)
            return

        attempt = event.attempt
        # attempt=1 → index 0 (30s), attempt=2 → index 1 (5m), attempt=3 → index 2 (1h)
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
                    "ingest_retry_scheduled",
                    job_id=event.job_id,
                    next_attempt=attempt + 1,
                    topic=retry_topic.value,
                    error_code=error.code,
                )
                return
            except KafkaError as exc:
                logger.error("retry_send_failed", error=str(exc), job_id=event.job_id)
                # Fall through to DLQ

        await self._send_to_dlq(
            event.model_dump_json().encode(),
            reason=error.code,
            detail=error.message,
        )
        logger.error(
            "ingest_sent_to_dlq",
            job_id=event.job_id,
            attempt=attempt,
            error_code=error.code,
        )


# ===========================================================================
# Process entry point
# ===========================================================================

async def run_worker() -> None:
    """Start the scout worker process, blocking until shutdown signal."""
    settings = get_settings()
    configure_logging()
    configure_tracing(service_name="trendstorm-scout")
    logger.info("scout_worker_booting")

    mongo = MongoClient(settings.mongo)
    redis = RedisClient(settings.redis)
    producer = KafkaProducerClient(settings.kafka)
    minio = MinioClient(settings.blob)

    await asyncio.gather(
        mongo.connect(),
        redis.connect(),
        producer.start(),
        minio.connect(),
    )

    source_repo = MongoSourceRepository(mongo)
    raw_doc_repo = MongoRawDocumentRepository(mongo)
    idem = IdempotencyRepository(mongo)

    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.ingest.fetch_timeout_seconds),
        headers={"User-Agent": settings.ingest.user_agent},
        max_redirects=settings.ingest.max_redirects,
        follow_redirects=True,
    )

    worker = ScoutWorker(
        kafka_settings=settings.kafka,
        source_repo=source_repo,
        raw_doc_repo=raw_doc_repo,
        idempotency=idem,
        producer=producer,
        minio=minio,
        http_client=http_client,
        redis=redis,
        ingest_settings=settings.ingest,
    )

    await worker.start()
    worker.install_signal_handlers()

    try:
        await worker.run()
    finally:
        logger.info("scout_worker_shutting_down")
        await worker.stop()
        await http_client.aclose()
        await producer.stop()
        await minio.close()
        await redis.close()
        await mongo.close()
        shutdown_tracing()


def main() -> None:
    """Run the worker synchronously — entry point for `python -m`."""
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_worker())


if __name__ == "__main__":
    main()
