"""Memory consolidation worker — episodic + semantic memory extraction (Phase 15.5).

Consumes `trendstorm.memory.pending.v1`. For each event:
    1. Loads Analysis and Category from Mongo.
    2. Writes one episodic memory (idempotent).
    3. Extracts N semantic memories via a lightweight LLM call.
    4. Persists all memories to Mongo + ChromaDB.
    5. Publishes MemoryCompletedEvent to `memory.completed.v1`.

Memory failure is NON-BLOCKING. If this worker fails permanently (DLQ),
the job is still marked COMPLETED by the orchestrator — the report is already
published. This is intentional: memory enriches future analyses but must not
gate user-visible job completion.

Idempotency key: `memory:{job_id}` — one-shot per memory consolidation request.

Retry topology:
    attempt 1 → RETRY_MEMORY_30S
    attempt 2 → RETRY_MEMORY_5M
    attempt 3 → RETRY_MEMORY_1H
    attempt 4+ → DLQ

Run:
    python -m trendstorm.orchestration.workers.memory_consolidation_worker
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.propagate import inject

from trendstorm.infrastructure.kafka.consumer import BaseConsumer
from trendstorm.infrastructure.kafka.producer import KafkaProducerClient
from trendstorm.infrastructure.mongo.client import MongoClient
from trendstorm.infrastructure.mongo.repositories import (
    IdempotencyRepository,
    MongoAnalysisRepository,
    MongoCategoryRepository,
    MongoMemoryRepository,
)
from trendstorm.infrastructure.vectors.chroma_memory_store import ChromaMemoryStore
from trendstorm.orchestration.events import MemoryCompletedEvent, MemoryPendingEvent
from trendstorm.orchestration.topics import ConsumerGroup, Topic
from trendstorm.services.memory.episodic_writer import EpisodicMemoryWriter
from trendstorm.services.memory.semantic_extractor import SemanticMemoryExtractor
from trendstorm.shared.config import get_settings
from trendstorm.shared.errors import NotFoundError
from trendstorm.shared.logging import configure_logging, get_logger
from trendstorm.shared.tracing import configure_tracing, shutdown_tracing
from trendstorm.shared.tracing.semantics import Attr

if TYPE_CHECKING:
    from trendstorm.orchestration.events import EventEnvelope
    from trendstorm.shared.config import KafkaSettings

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)

_RETRY_TOPICS = [
    Topic.RETRY_MEMORY_30S,
    Topic.RETRY_MEMORY_5M,
    Topic.RETRY_MEMORY_1H,
]


class MemoryConsolidationWorker(BaseConsumer):
    """Consumes MemoryPendingEvents and writes episodic + semantic memories."""

    def __init__(
        self,
        *,
        kafka_settings: KafkaSettings,
        episodic_writer: EpisodicMemoryWriter,
        semantic_extractor: SemanticMemoryExtractor,
        analysis_repo: MongoAnalysisRepository,
        category_repo: MongoCategoryRepository,
        idempotency: IdempotencyRepository,
        producer: KafkaProducerClient,
    ) -> None:
        super().__init__(
            topics=[Topic.MEMORY_PENDING],
            group_id=ConsumerGroup.MEMORY_CONSOLIDATION.value,
            settings=kafka_settings,
            idempotency=idempotency,
            producer=producer,
            worker_name="memory_consolidation",
        )
        self._episodic_writer = episodic_writer
        self._semantic_extractor = semantic_extractor
        self._analysis_repo = analysis_repo
        self._category_repo = category_repo

    def _idempotency_key(self, event: EventEnvelope) -> str | None:
        if isinstance(event, MemoryPendingEvent):
            return f"memory:{event.job_id}"
        return f"memory:{event.event_id}"

    def _retry_topics(self) -> list[Topic]:
        return _RETRY_TOPICS

    async def handle(self, event: EventEnvelope) -> None:
        if not isinstance(event, MemoryPendingEvent):
            logger.warning(
                "memory_unexpected_event_type",
                event_type=getattr(event, "event_type", "unknown"),
            )
            return

        with tracer.start_as_current_span(
            "memory_consolidation.process",
            attributes={
                Attr.JOB_ID: event.job_id,
                Attr.ANALYSIS_ID: event.analysis_id,
                Attr.CATEGORY_ID: event.category_id,
                Attr.ATTEMPT: event.attempt,
            },
        ):
            await self._process(event)

    async def _process(self, event: MemoryPendingEvent) -> None:
        tenant_id = event.tenant_id
        job_id = event.job_id
        analysis_id = event.analysis_id
        category_id = event.category_id

        # Load analysis.
        analysis = await self._analysis_repo.get(tenant_id, analysis_id)
        if analysis is None:
            raise NotFoundError(
                f"Analysis {analysis_id} not found for memory consolidation",
                context={"job_id": job_id, "analysis_id": analysis_id},
            )

        # Load category (needed for semantic extractor context).
        category = await self._category_repo.get(tenant_id, category_id)
        if category is None:
            raise NotFoundError(
                f"Category {category_id} not found for memory consolidation",
                context={"job_id": job_id, "category_id": category_id},
            )

        # Write episodic + semantic memories concurrently.
        # Each is independently idempotent and independently fail-safe.
        episodic_result, semantic_result = await asyncio.gather(
            self._episodic_writer.write(
                analysis=analysis,
                tenant_id=tenant_id,
                job_id=job_id,
                category_id=category_id,
            ),
            self._semantic_extractor.extract_and_store(
                analysis=analysis,
                tenant_id=tenant_id,
                job_id=job_id,
                category_id=category_id,
            ),
            return_exceptions=True,
        )

        # Collect results — exceptions are logged but not re-raised.
        episodic_id: str | None = None
        semantic_ids: list[str] = []

        if isinstance(episodic_result, BaseException):
            logger.error(
                "memory.episodic.failed",
                job_id=job_id,
                error=str(episodic_result),
            )
        elif episodic_result is not None:
            episodic_id = episodic_result.id

        if isinstance(semantic_result, BaseException):
            logger.error(
                "memory.semantic.failed",
                job_id=job_id,
                error=str(semantic_result),
            )
        elif isinstance(semantic_result, list):
            semantic_ids = [m.id for m in semantic_result]

        # Publish MemoryCompletedEvent regardless of partial failures.
        otel_carrier: dict[str, str] = {}
        inject(otel_carrier)
        completed_event = MemoryCompletedEvent(
            correlation_id=event.correlation_id,
            tenant_id=tenant_id,
            traceparent=otel_carrier.get("traceparent"),
            job_id=job_id,
            success=True,
            episodic_memory_id=episodic_id,
            semantic_memory_ids=semantic_ids,
        )
        await self._producer.producer.send_and_wait(
            Topic.MEMORY_COMPLETED.value,
            value=completed_event.model_dump_json().encode(),
            key=job_id.encode(),
        )
        logger.info(
            "memory_consolidation.completed",
            job_id=job_id,
            episodic_id=episodic_id,
            n_semantic=len(semantic_ids),
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def run_worker() -> None:
    settings = get_settings()
    configure_logging()
    configure_tracing(service_name="trendstorm-memory-consolidation")

    mongo = MongoClient(settings.mongo)
    await mongo.connect()

    chroma_memory_store = ChromaMemoryStore(settings.vector)
    await chroma_memory_store.connect()

    from trendstorm.infrastructure.llm.registry import build_chat_provider, build_embedding_provider

    embed = build_embedding_provider(settings)
    chat = build_chat_provider(settings)

    memory_repo = MongoMemoryRepository(mongo)
    analysis_repo = MongoAnalysisRepository(mongo)
    category_repo = MongoCategoryRepository(mongo)
    idempotency_repo = IdempotencyRepository(mongo)

    episodic_writer = EpisodicMemoryWriter(
        embed=embed,
        vector_store=chroma_memory_store,
        memory_repo=memory_repo,
    )
    semantic_extractor = SemanticMemoryExtractor(
        chat_provider=chat,
        embed=embed,
        vector_store=chroma_memory_store,
        memory_repo=memory_repo,
        max_memories_per_job=settings.memory.max_semantic_memories_per_job,
        supersede_threshold=settings.memory.supersede_similarity_threshold,
    )

    producer = KafkaProducerClient(settings.kafka)
    await producer.start()

    worker = MemoryConsolidationWorker(
        kafka_settings=settings.kafka,
        episodic_writer=episodic_writer,
        semantic_extractor=semantic_extractor,
        analysis_repo=analysis_repo,
        category_repo=category_repo,
        idempotency=idempotency_repo,
        producer=producer,
    )

    from trendstorm.infrastructure.metrics.prometheus_server import MetricsServer

    metrics_server = MetricsServer()
    await metrics_server.start()

    try:
        await worker.run()
    finally:
        await metrics_server.stop()
        await producer.stop()
        await mongo.close()
        await chroma_memory_store.close()
        shutdown_tracing()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
