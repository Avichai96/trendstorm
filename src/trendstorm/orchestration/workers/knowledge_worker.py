"""Knowledge worker — chunking and embedding Kafka consumer.

Consumes `trendstorm.knowledge.pending.v1`, processes each document through
`KnowledgePipeline` (text download → chunking → embedding → vector store),
and publishes one `trendstorm.knowledge.completed.v1` event per job.

Idempotency key: `f"knowledge:{event.job_id}"` — one handler processes ALL
documents for a job. Per-document idempotency is handled inside
KnowledgePipeline.process_document() via list_by_document (skips if already
chunked).

Concurrency: bounded asyncio.Queue producer-consumer with N workers (default 4).
This bounds the number of simultaneous embedding API calls, which matter more
than raw I/O here given rate-limit budgets on cloud providers.

Retry topology:
    attempt 1 → RETRY_KNOWLEDGE_30S  (attempt becomes 2)
    attempt 2 → RETRY_KNOWLEDGE_5M   (attempt becomes 3)
    attempt 3 → RETRY_KNOWLEDGE_1H   (attempt becomes 4)
    attempt 4+ → DLQ

Only raised exceptions trigger the retry path. Per-document failures within
the pipeline are reported in failed_document_ids and do NOT cause retries.

Run:
    python -m trendstorm.orchestration.workers.knowledge_worker
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from aiokafka.errors import KafkaError
from opentelemetry import trace
from opentelemetry.propagate import inject

from trendstorm.agents.knowledge.chunker import ParentChildChunker
from trendstorm.agents.knowledge.pipeline import KnowledgePipeline
from trendstorm.domain.streaming.events import StreamEvent, StreamEventType
from trendstorm.infrastructure.blob.minio_client import MinioClient
from trendstorm.infrastructure.kafka.consumer import BaseConsumer
from trendstorm.infrastructure.kafka.producer import KafkaProducerClient
from trendstorm.infrastructure.mongo.client import MongoClient
from trendstorm.infrastructure.mongo.repositories import (
    IdempotencyRepository,
    MongoChunkRepository,
)
from trendstorm.infrastructure.vectors.chroma_store import ChromaVectorStore
from trendstorm.orchestration.events import (
    KnowledgeCompletedEvent,
    KnowledgeDocResult,
    KnowledgePendingEvent,
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
    from trendstorm.shared.config import KafkaSettings
    from trendstorm.shared.errors import TrendStormError

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)

_DEFAULT_CONCURRENCY = 4

_RETRY_TOPICS = [
    Topic.RETRY_KNOWLEDGE_30S,
    Topic.RETRY_KNOWLEDGE_5M,
    Topic.RETRY_KNOWLEDGE_1H,
]


class KnowledgeWorker(BaseConsumer):
    """Consumes KnowledgePendingEvents and runs the chunking+embedding pipeline."""

    def __init__(
        self,
        *,
        kafka_settings: KafkaSettings,
        pipeline: KnowledgePipeline,
        idempotency: IdempotencyRepository,
        producer: KafkaProducerClient,
        concurrency: int = _DEFAULT_CONCURRENCY,
    ) -> None:
        super().__init__(
            topics=[Topic.KNOWLEDGE_PENDING],
            group_id=ConsumerGroup.KNOWLEDGE.value,
            settings=kafka_settings,
            idempotency=idempotency,
            producer=producer,
            worker_name="knowledge",
        )
        self._pipeline = pipeline
        self._concurrency = concurrency

    # ------------------------------------------------------------------ #
    # Idempotency                                                          #
    # ------------------------------------------------------------------ #

    def _idempotency_key(self, event: EventEnvelope) -> str | None:
        if isinstance(event, KnowledgePendingEvent):
            return f"knowledge:{event.job_id}"
        return f"knowledge:{event.event_id}"

    # ------------------------------------------------------------------ #
    # Main handler                                                         #
    # ------------------------------------------------------------------ #

    async def handle(self, event: EventEnvelope) -> None:
        if not isinstance(event, KnowledgePendingEvent):
            logger.warning(
                "knowledge_unexpected_event_type",
                event_type=getattr(event, "event_type", "unknown"),
            )
            return

        with tracer.start_as_current_span(
            "knowledge.embed_job",
            attributes={
                Attr.JOB_ID: event.job_id,
                Attr.ATTEMPT: event.attempt,
                "trendstorm.document_count": len(event.document_refs),
            },
        ):
            await self._embed(event)

    async def _embed(self, event: KnowledgePendingEvent) -> None:
        await emit_stream_event(
            StreamEvent(
                job_id=event.job_id,
                tenant_id=event.tenant_id,
                event_type=StreamEventType.STAGE_STARTED,
                stage="embedding",
                payload={"document_count": len(event.document_refs)},
            ),
            producer=self._producer,
            correlation_id=event.correlation_id,
        )

        results, failed_ids = await self._process_all(event)

        otel_carrier: dict[str, str] = {}
        inject(otel_carrier)

        completed = KnowledgeCompletedEvent(
            correlation_id=event.correlation_id,
            tenant_id=event.tenant_id,
            traceparent=otel_carrier.get("traceparent"),
            job_id=event.job_id,
            document_results=results,
            failed_document_ids=failed_ids,
        )
        await self._producer.producer.send_and_wait(
            Topic.KNOWLEDGE_COMPLETED.value,
            value=completed.model_dump_json().encode(),
            key=event.job_id.encode(),
        )
        logger.info(
            "knowledge_completed_published",
            job_id=event.job_id,
            n_docs=len(results),
            n_failed=len(failed_ids),
        )

        n_chunks = sum(r.n_chunks for r in results)
        n_vectors = sum(r.n_vectors for r in results)
        await emit_stream_event(
            StreamEvent(
                job_id=event.job_id,
                tenant_id=event.tenant_id,
                event_type=StreamEventType.STAGE_COMPLETED,
                stage="embedding",
                payload={
                    "docs_processed": len(results),
                    "chunks_created": n_chunks,
                    "vectors_upserted": n_vectors,
                    "failed": len(failed_ids),
                },
            ),
            producer=self._producer,
            correlation_id=event.correlation_id,
        )

    async def _process_all(
        self, event: KnowledgePendingEvent
    ) -> tuple[list[KnowledgeDocResult], list[str]]:
        """Process all document_refs with bounded concurrency."""
        queue: asyncio.Queue[int] = asyncio.Queue()
        for i in range(len(event.document_refs)):
            queue.put_nowait(i)

        results: list[KnowledgeDocResult] = []
        failed_ids: list[str] = []

        async def _worker() -> None:
            while True:
                try:
                    idx = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                ref = event.document_refs[idx]
                try:
                    pipeline_result = await self._pipeline.process_document(
                        document_id=ref.document_id,
                        blob_uri_text=ref.blob_uri_text,
                        tenant_id=event.tenant_id,
                        job_id=event.job_id,
                        category_id=ref.category_id,
                        source_id=ref.source_id,
                    )
                    results.append(
                        KnowledgeDocResult(
                            document_id=ref.document_id,
                            n_chunks=pipeline_result.n_chunks_created,
                            n_vectors=pipeline_result.n_vectors_upserted,
                            skipped=pipeline_result.skipped,
                        )
                    )
                except Exception as exc:
                    logger.error(
                        "knowledge_document_failed",
                        document_id=ref.document_id,
                        job_id=event.job_id,
                        error=str(exc),
                    )
                    failed_ids.append(ref.document_id)
                finally:
                    queue.task_done()

        n_workers = min(self._concurrency, len(event.document_refs))
        tasks = [asyncio.create_task(_worker()) for _ in range(n_workers)]
        await queue.join()
        for t in tasks:
            t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*tasks, return_exceptions=True)

        return results, failed_ids

    # ------------------------------------------------------------------ #
    # Retry routing                                                        #
    # ------------------------------------------------------------------ #

    async def _handle_failure(self, event: EventEnvelope, error: TrendStormError) -> None:
        if not isinstance(event, KnowledgePendingEvent):
            await super()._handle_failure(event, error)
            return

        attempt = event.attempt
        retry_index = attempt - 1
        if retry_index < len(_RETRY_TOPICS):
            retry_topic = _RETRY_TOPICS[retry_index]
            retry_event = event.model_copy(update={"attempt": attempt + 1, "event_id": new_id()})
            try:
                await self._producer.producer.send_and_wait(
                    retry_topic.value,
                    value=retry_event.model_dump_json().encode(),
                    key=event.job_id.encode(),
                )
                logger.warning(
                    "knowledge_retry_scheduled",
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
            "knowledge_sent_to_dlq",
            job_id=event.job_id,
            attempt=attempt,
            error_code=error.code,
        )


# ===========================================================================
# Process entry point
# ===========================================================================


async def run_worker() -> None:
    """Start the knowledge worker process, blocking until shutdown signal."""
    settings = get_settings()
    configure_logging()
    configure_tracing(service_name="trendstorm-knowledge")
    logger.info("knowledge_worker_booting")

    from trendstorm.infrastructure.llm.registry import build_embedding_provider  # deferred

    mongo = MongoClient(settings.mongo)
    minio = MinioClient(settings.blob)
    chroma = ChromaVectorStore(settings.vector)
    producer = KafkaProducerClient(settings.kafka)

    await asyncio.gather(
        mongo.connect(),
        minio.connect(),
        chroma.connect(),
        producer.start(),
    )

    embedding_provider = build_embedding_provider(settings)
    chunk_repo = MongoChunkRepository(mongo)
    idem = IdempotencyRepository(mongo)

    pipeline = KnowledgePipeline(
        chunker=ParentChildChunker(
            parent_size_tokens=settings.vector.embedding_dimensions * 2,
            child_size_tokens=settings.vector.embedding_dimensions // 2,
        ),
        embedding_provider=embedding_provider,
        chunk_repo=chunk_repo,
        vector_store=chroma,
        minio=minio,
    )

    worker = KnowledgeWorker(
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
        logger.info("knowledge_worker_shutting_down")
        await worker.stop()
        await producer.stop()
        await chroma.close()
        await minio.close()
        await mongo.close()
        shutdown_tracing()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_worker())


if __name__ == "__main__":
    main()
