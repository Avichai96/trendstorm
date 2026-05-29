"""Analyst worker — retrieval + analysis + validation Kafka consumer.

Consumes `trendstorm.analysis.pending.v1`, runs one full Analyst pass
(HybridRetriever → ChatProvider → AnalysisValidator), persists the Analysis
to Mongo, and publishes one `trendstorm.analysis.completed.v1` event.

Idempotency key includes refinement_loop so different refinement attempts
are NOT collapsed by the dedup layer:

    f"analyst:{event.job_id}:{event.refinement_loop}"

Retry topology (Kafka-level, NOT refinement-level):
    attempt 1 → RETRY_ANALYSIS_30S  (attempt becomes 2)
    attempt 2 → RETRY_ANALYSIS_5M   (attempt becomes 3)
    attempt 3 → RETRY_ANALYSIS_1H   (attempt becomes 4)
    attempt 4+ → DLQ

Refinement is orchestrator-controlled: the worker just runs one pass per
incoming event. The orchestrator inspects passed + refinement_loop +
max_refinement_loops and publishes a new AnalysisPendingEvent if it decides
to refine.

Failure semantics:
    - LLMPermanentError / LLMSchemaError / ValidationError → publish a
      completed event with success=False and the error code; let the
      orchestrator decide whether to fail the job or downgrade gracefully.
      Do NOT take the Kafka retry path for these — they will not get better.
    - LLMTransientError / Kafka errors / other Exceptions → re-raise so the
      BaseConsumer retry topology picks them up.

Run:
    python -m trendstorm.orchestration.workers.analyst_worker
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, cast

from aiokafka.errors import KafkaError
from opentelemetry import trace
from opentelemetry.propagate import inject

from trendstorm.domain.streaming.events import StreamEvent, StreamEventType
from trendstorm.infrastructure.kafka.consumer import BaseConsumer
from trendstorm.infrastructure.kafka.producer import KafkaProducerClient
from trendstorm.infrastructure.mongo.client import MongoClient
from trendstorm.infrastructure.mongo.repositories import (
    IdempotencyRepository,
    MongoAnalysisRepository,
    MongoCategoryRepository,
)
from trendstorm.infrastructure.mongo.repositories.cost_ledger_repository import (
    MongoCostLedgerRepository,
)
from trendstorm.infrastructure.vectors.chroma_store import ChromaVectorStore
from trendstorm.orchestration.events import (
    AnalysisCompletedEvent,
    AnalysisPendingEvent,
    EvalSampleEvent,
)
from trendstorm.orchestration.topics import ConsumerGroup, Topic
from trendstorm.services.streaming.emit import emit_stream_event
from trendstorm.shared.config import get_settings
from trendstorm.shared.errors import (
    LLMPermanentError,
    LLMSchemaError,
    NotFoundError,
    TrendStormError,
    ValidationError,
)
from trendstorm.shared.ids import new_id
from trendstorm.shared.logging import configure_logging, get_logger
from trendstorm.shared.metrics.registry import METRICS
from trendstorm.shared.tracing import configure_tracing, shutdown_tracing
from trendstorm.shared.tracing.semantics import Attr

if TYPE_CHECKING:
    from trendstorm.orchestration.events import EventEnvelope
    from trendstorm.services.analysis.analyst import Analyst
    from trendstorm.shared.config import KafkaSettings

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)

_RETRY_TOPICS = [
    Topic.RETRY_ANALYSIS_30S,
    Topic.RETRY_ANALYSIS_5M,
    Topic.RETRY_ANALYSIS_1H,
]


class AnalystWorker(BaseConsumer):
    """Consumes AnalysisPendingEvents and runs one Analyst pass per event."""

    def __init__(
        self,
        *,
        kafka_settings: KafkaSettings,
        analyst: Analyst,
        analysis_repo: MongoAnalysisRepository,
        category_repo: MongoCategoryRepository,
        idempotency: IdempotencyRepository,
        producer: KafkaProducerClient,
        ledger_repo: MongoCostLedgerRepository | None = None,
    ) -> None:
        super().__init__(
            topics=[Topic.ANALYSIS_PENDING],
            group_id=ConsumerGroup.ANALYST.value,
            settings=kafka_settings,
            idempotency=idempotency,
            producer=producer,
            worker_name="analyst",
        )
        self._analyst = analyst
        self._analysis_repo = analysis_repo
        self._category_repo = category_repo
        self._ledger_repo = ledger_repo

    # ------------------------------------------------------------------ #
    # Idempotency
    # ------------------------------------------------------------------ #

    def _idempotency_key(self, event: EventEnvelope) -> str | None:
        # Refinement loops are SEPARATE work items — each gets its own key.
        if isinstance(event, AnalysisPendingEvent):
            return f"analyst:{event.job_id}:{event.refinement_loop}"
        return f"analyst:{event.event_id}"

    # ------------------------------------------------------------------ #
    # Main handler
    # ------------------------------------------------------------------ #

    async def handle(self, event: EventEnvelope) -> None:
        if not isinstance(event, AnalysisPendingEvent):
            logger.warning(
                "analyst_unexpected_event_type",
                event_type=getattr(event, "event_type", "unknown"),
            )
            return

        with tracer.start_as_current_span(
            "analyst.run_pass",
            attributes={
                Attr.JOB_ID: event.job_id,
                Attr.CATEGORY_ID: event.category_id,
                Attr.REFINEMENT_LOOP: event.refinement_loop,
                Attr.ATTEMPT: event.attempt,
            },
        ):
            await self._run_pass(event)

    def _record_handle_metrics(self, event: EventEnvelope, status: str, elapsed: float) -> None:
        METRICS.analyst_passes.labels(tenant_id=event.tenant_id, status=status).inc()
        METRICS.analyst_pass_duration.labels(tenant_id=event.tenant_id, status=status).observe(
            elapsed
        )

    async def _run_pass(self, event: AnalysisPendingEvent) -> None:
        """One Analyst pass. Persists, then publishes completion."""
        await emit_stream_event(
            StreamEvent(
                job_id=event.job_id,
                tenant_id=event.tenant_id,
                event_type=StreamEventType.STAGE_STARTED,
                stage="analyzing",
                payload={"refinement_loop": event.refinement_loop},
            ),
            producer=self._producer,
            correlation_id=event.correlation_id,
        )

        try:
            category = await self._category_repo.get(event.tenant_id, event.category_id)
            if category is None:
                # Treat as permanent — retry won't fix a missing category.
                raise NotFoundError(
                    f"Category {event.category_id} not found",
                    context={"tenant_id": event.tenant_id, "category_id": event.category_id},
                )

            result = await self._analyst.produce_analysis(
                category,
                tenant_id=event.tenant_id,
                job_id=event.job_id,
                refinement_notes=event.refinement_notes,
                refinement_loop=event.refinement_loop,
                ledger=self._ledger_repo,
            )

            # Persist the Analysis BEFORE publishing the completion event —
            # consumers (orchestrator) MUST be able to load it by analysis_id.
            await self._analysis_repo.insert(result.analysis)

            await self._publish_completed(
                event,
                success=True,
                analysis_id=result.analysis.id,
                passed=result.validation.passed,
                score=result.validation.score,
            )
            logger.info(
                "analyst_pass_published",
                job_id=event.job_id,
                analysis_id=result.analysis.id,
                refinement_loop=event.refinement_loop,
                passed=result.validation.passed,
                score=result.validation.score,
            )

            # 1% production eval sampling — deterministic hash so the same job
            # is always sampled or always skipped, regardless of retries.
            if result.validation.passed and hash(event.job_id) % 100 == 0:
                await self._publish_eval_sample(event, analysis_id=result.analysis.id)

            await emit_stream_event(
                StreamEvent(
                    job_id=event.job_id,
                    tenant_id=event.tenant_id,
                    event_type=StreamEventType.STAGE_COMPLETED,
                    stage="analyzing",
                    payload={
                        "analysis_id": result.analysis.id,
                        "passed": result.validation.passed,
                        "score": result.validation.score,
                        "refinement_loop": event.refinement_loop,
                        "n_insights": len(result.analysis.insights),
                    },
                ),
                producer=self._producer,
                correlation_id=event.correlation_id,
            )

        except (LLMPermanentError, LLMSchemaError, ValidationError, NotFoundError) as exc:
            # Permanent failures: report up to the orchestrator, do NOT retry.
            # The orchestrator can decide to fail the job or publish a
            # low-confidence result depending on refinement_loop state.
            logger.error(
                "analyst_permanent_failure",
                job_id=event.job_id,
                refinement_loop=event.refinement_loop,
                error_code=exc.code,
                error=exc.message,
            )
            await self._publish_completed(
                event,
                success=False,
                error_code=exc.code,
                error_message=exc.message,
            )

    async def _publish_eval_sample(
        self,
        event: AnalysisPendingEvent,
        *,
        analysis_id: str,
    ) -> None:
        """Publish an EvalSampleEvent for 1% production eval sampling."""
        otel_carrier: dict[str, str] = {}
        inject(otel_carrier)
        sample = EvalSampleEvent(
            correlation_id=event.correlation_id,
            tenant_id=event.tenant_id,
            traceparent=otel_carrier.get("traceparent"),
            job_id=event.job_id,
            analysis_id=analysis_id,
        )
        try:
            await self._producer.producer.send_and_wait(
                Topic.EVAL_SAMPLE.value,
                value=sample.model_dump_json().encode(),
                key=event.job_id.encode(),
            )
            logger.info(
                "analyst_eval_sample_published",
                job_id=event.job_id,
                analysis_id=analysis_id,
            )
        except Exception as exc:
            # Eval sampling is best-effort — failure must never crash the
            # business logic or trigger a Kafka retry.
            logger.warning(
                "analyst_eval_sample_failed",
                job_id=event.job_id,
                error=str(exc),
            )

    async def _publish_completed(
        self,
        event: AnalysisPendingEvent,
        *,
        success: bool,
        analysis_id: str | None = None,
        passed: bool = False,
        score: float = 0.0,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        otel_carrier: dict[str, str] = {}
        inject(otel_carrier)

        completed = AnalysisCompletedEvent(
            correlation_id=event.correlation_id,
            tenant_id=event.tenant_id,
            traceparent=otel_carrier.get("traceparent"),
            job_id=event.job_id,
            success=success,
            analysis_id=analysis_id,
            passed=passed,
            score=score,
            refinement_loop=event.refinement_loop,
            error_code=error_code,
            error_message=error_message,
        )
        await self._producer.producer.send_and_wait(
            Topic.ANALYSIS_COMPLETED.value,
            value=completed.model_dump_json().encode(),
            key=event.job_id.encode(),
        )

    # ------------------------------------------------------------------ #
    # Retry routing — same tiered topology as Scout / Knowledge
    # ------------------------------------------------------------------ #

    async def _handle_failure(self, event: EventEnvelope, error: TrendStormError) -> None:
        if not isinstance(event, AnalysisPendingEvent):
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
                    "analyst_retry_scheduled",
                    job_id=event.job_id,
                    refinement_loop=event.refinement_loop,
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
            "analyst_sent_to_dlq",
            job_id=event.job_id,
            attempt=attempt,
            error_code=error.code,
        )


# ===========================================================================
# Process entry point
# ===========================================================================


async def run_worker() -> None:
    """Start the analyst worker process, blocking until shutdown signal."""
    settings = get_settings()
    configure_logging()
    configure_tracing(service_name="trendstorm-analyst")
    logger.info("analyst_worker_booting")

    # Deferred imports — heavy LLM/retrieval modules only needed on the actual
    # worker process, not on tests that import this module for its types.
    from trendstorm.domain.llm.providers import ChatProvider
    from trendstorm.infrastructure.llm.registry import (
        build_chat_provider,
        build_embedding_provider,
    )
    from trendstorm.infrastructure.retrieval.chroma_vector import ChromaVectorRetriever
    from trendstorm.infrastructure.retrieval.cohere_reranker import CohereReranker
    from trendstorm.infrastructure.retrieval.mongo_bm25 import MongoBM25Retriever
    from trendstorm.services.analysis.analyst import Analyst
    from trendstorm.services.analysis.validator import AnalysisValidator
    from trendstorm.services.retrieval.hybrid import HybridRetriever
    from trendstorm.services.retrieval.query_expansion import QueryExpander

    mongo = MongoClient(settings.mongo)
    chroma = ChromaVectorStore(settings.vector)
    producer = KafkaProducerClient(settings.kafka)

    await asyncio.gather(
        mongo.connect(),
        chroma.connect(),
        producer.start(),
    )

    chat_provider = build_chat_provider(settings)
    embedding_provider = build_embedding_provider(settings)

    bm25 = MongoBM25Retriever(mongo)
    vector = ChromaVectorRetriever(chroma, embedding_provider)

    reranker: CohereReranker | None = None
    cohere_key = settings.llm.cohere_api_key.get_secret_value()
    if cohere_key:
        reranker = CohereReranker(api_key=cohere_key, model=settings.llm.cohere_rerank_model)
        await reranker.connect()
        logger.info("analyst_cohere_reranker_enabled", model=settings.llm.cohere_rerank_model)
    else:
        logger.warning("analyst_cohere_disabled_no_key")

    expander = QueryExpander(cast(ChatProvider, chat_provider))
    retriever = HybridRetriever(
        bm25=bm25,
        vector=vector,
        expander=expander,
        mongo=mongo,
        settings=settings.analysis,
        reranker=reranker,
    )

    from trendstorm.infrastructure.mongo.repositories import MongoMemoryRepository
    from trendstorm.infrastructure.vectors.chroma_memory_store import ChromaMemoryStore
    from trendstorm.services.memory.retrieval import MemoryRetriever

    chroma_memory = ChromaMemoryStore(settings.vector)
    await chroma_memory.connect()
    memory_repo = MongoMemoryRepository(mongo)
    memory_retriever = MemoryRetriever(
        embed=embedding_provider,
        vector_store=chroma_memory,
        memory_repo=memory_repo,
    )

    validator = AnalysisValidator(chat_provider, settings.analysis)
    analyst = Analyst(
        retriever,
        chat_provider,
        validator,
        settings.analysis,
        memory_retriever=memory_retriever,
        memory_final_k=settings.memory.memory_final_k,
    )

    analysis_repo = MongoAnalysisRepository(mongo)
    category_repo = MongoCategoryRepository(mongo)
    idem = IdempotencyRepository(mongo)
    ledger_repo = MongoCostLedgerRepository(mongo)

    worker = AnalystWorker(
        kafka_settings=settings.kafka,
        analyst=analyst,
        analysis_repo=analysis_repo,
        category_repo=category_repo,
        idempotency=idem,
        producer=producer,
        ledger_repo=ledger_repo,
    )
    await worker.start()
    worker.install_signal_handlers()

    try:
        await worker.run()
    finally:
        logger.info("analyst_worker_shutting_down")
        await worker.stop()
        await producer.stop()
        if reranker is not None:
            await reranker.close()
        await chroma.close()
        await chroma_memory.close()
        await mongo.close()
        shutdown_tracing()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_worker())


if __name__ == "__main__":
    main()
