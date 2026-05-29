"""Production eval worker — evaluates 1% sampled production analyses.

Consumes `trendstorm.eval.sample.v1`, runs the configured evaluators against
the sampled Analysis, and persists an EvaluationResult to MongoDB.

Evaluator set:
    - CitationLookupEvaluator (always enabled — deterministic, no API key)
    - LLMPanelFaithfulnessEvaluator (enabled if ≥ 2 LLM providers have keys)
    - LLMPanelRelevanceEvaluator (same condition)
    - GoldenCoverageEvaluator — intentionally omitted for production samples
      (production analyses have no golden expected_analysis)

LangSmith integration:
    Results are best-effort pushed to `settings.eval.langsmith_project_prod`.
    If the API key is absent or the push fails, evaluation still succeeds and
    the result is persisted to MongoDB.

Idempotency key: `f"prod_eval:{event.job_id}:{event.analysis_id}"`.
A given analysis is only evaluated once even if the EvalSampleEvent is
delivered more than once.

Run:
    python -m trendstorm.orchestration.workers.production_eval_worker
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from opentelemetry import trace

from trendstorm.agents.production_eval.pipeline import ProductionEvalPipeline
from trendstorm.infrastructure.kafka.consumer import BaseConsumer
from trendstorm.infrastructure.kafka.producer import KafkaProducerClient
from trendstorm.infrastructure.mongo.client import MongoClient
from trendstorm.infrastructure.mongo.repositories import (
    IdempotencyRepository,
    MongoAnalysisRepository,
    MongoChunkRepository,
)
from trendstorm.infrastructure.vectors.chroma_store import ChromaVectorStore
from trendstorm.orchestration.events import EvalSampleEvent
from trendstorm.orchestration.topics import ConsumerGroup, Topic
from trendstorm.services.evaluation.runner import EvalRunner
from trendstorm.shared.config import get_settings
from trendstorm.shared.logging import configure_logging, get_logger
from trendstorm.shared.tracing import configure_tracing, shutdown_tracing
from trendstorm.shared.tracing.semantics import Attr

if TYPE_CHECKING:
    from trendstorm.orchestration.events import EventEnvelope
    from trendstorm.shared.config import KafkaSettings

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)


class ProductionEvalWorker(BaseConsumer):
    """Evaluates sampled production analyses from eval.sample.v1."""

    def __init__(
        self,
        *,
        kafka_settings: KafkaSettings,
        pipeline: ProductionEvalPipeline,
        idempotency: IdempotencyRepository,
        producer: KafkaProducerClient,
    ) -> None:
        super().__init__(
            topics=[Topic.EVAL_SAMPLE],
            group_id=ConsumerGroup.PRODUCTION_EVAL.value,
            settings=kafka_settings,
            idempotency=idempotency,
            producer=producer,
            worker_name="production_eval",
        )
        self._pipeline = pipeline

    def _idempotency_key(self, event: EventEnvelope) -> str | None:
        if isinstance(event, EvalSampleEvent):
            # Scope to both job and analysis — the same job could theoretically
            # produce multiple analyses via refinement; each gets its own eval.
            return f"prod_eval:{event.job_id}:{event.analysis_id}"
        return f"prod_eval:{event.event_id}"

    async def handle(self, event: EventEnvelope) -> None:
        if not isinstance(event, EvalSampleEvent):
            logger.warning(
                "production_eval.unexpected_event_type",
                event_type=getattr(event, "event_type", "unknown"),
            )
            return

        with tracer.start_as_current_span(
            "production_eval.handle",
            attributes={
                Attr.JOB_ID: event.job_id,
                "analysis_id": event.analysis_id,
            },
        ):
            result = await self._pipeline.evaluate_analysis(
                tenant_id=event.tenant_id,
                analysis_id=event.analysis_id,
                job_id=event.job_id,
            )

            if result.skipped:
                logger.info(
                    "production_eval.skipped",
                    analysis_id=event.analysis_id,
                    reason=result.skip_reason,
                )
                return

            ev = result.evaluation_result
            logger.info(
                "production_eval.complete",
                analysis_id=event.analysis_id,
                aggregate_score=ev.aggregate_score,
                flagged=ev.flagged,
                n_dimensions=len(ev.dimension_scores),
            )


# ===========================================================================
# Process entry point
# ===========================================================================


async def run_worker() -> None:
    settings = get_settings()
    configure_logging()
    configure_tracing(service_name="trendstorm-production-eval")
    logger.info("production_eval_worker_booting")

    from trendstorm.infrastructure.langsmith.client import LangSmithClient
    from trendstorm.infrastructure.llm.registry import (
        build_chat_provider,
        build_embedding_provider,
    )
    from trendstorm.services.evaluation.evaluators.citation import CitationLookupEvaluator

    mongo = MongoClient(settings.mongo)
    chroma = ChromaVectorStore(settings.vector)
    producer = KafkaProducerClient(settings.kafka)

    await asyncio.gather(
        mongo.connect(),
        chroma.connect(),
        producer.start(),
    )

    from trendstorm.domain.evaluation.evaluator import Evaluator

    embed = build_embedding_provider(settings)
    evaluators: list[Evaluator] = [
        CitationLookupEvaluator(embed)
    ]  # CitationLookupEvaluator satisfies Evaluator structurally

    # Add LLM panel evaluators when enough providers have keys configured.
    from trendstorm.services.evaluation.evaluators.faithfulness import LLMPanelFaithfulnessEvaluator
    from trendstorm.services.evaluation.evaluators.relevance import LLMPanelRelevanceEvaluator
    from trendstorm.services.evaluation.panel import LLMPanel

    try:
        from typing import cast as _cast

        from trendstorm.domain.evaluation.judge import LLMJudge

        chat = build_chat_provider(settings)
        # StructuredChatProvider satisfies LLMJudge structurally at runtime
        # (concrete providers implement model_id + judge()); cast for mypy.
        panel = LLMPanel(judges=[_cast(LLMJudge, chat)], settings=settings.eval)
        evaluators.append(
            _cast(Evaluator, LLMPanelFaithfulnessEvaluator(panel))
        )  # satisfies Evaluator structurally
        evaluators.append(
            _cast(Evaluator, LLMPanelRelevanceEvaluator(panel))
        )  # satisfies Evaluator structurally
        logger.info("production_eval_llm_panel_enabled")
    except Exception as exc:
        logger.warning(
            "production_eval_llm_panel_disabled",
            reason=str(exc),
        )

    langsmith_client: LangSmithClient | None = None
    try:
        langsmith_client = LangSmithClient(settings.langsmith)
        await langsmith_client.connect()
        logger.info("production_eval_langsmith_connected")
    except Exception as exc:
        logger.warning("production_eval_langsmith_disabled", reason=str(exc))
        langsmith_client = None

    runner = EvalRunner(
        evaluators=evaluators,
        settings=settings.eval,
        langsmith=langsmith_client,
    )
    analysis_repo = MongoAnalysisRepository(mongo)
    chunk_repo = MongoChunkRepository(mongo)
    idem = IdempotencyRepository(mongo)

    pipeline = ProductionEvalPipeline(
        runner=runner,
        analysis_repo=analysis_repo,
        chunk_repo=chunk_repo,
    )

    worker = ProductionEvalWorker(
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
        logger.info("production_eval_worker_shutting_down")
        await worker.stop()
        await producer.stop()
        await chroma.close()
        await mongo.close()
        shutdown_tracing()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_worker())


if __name__ == "__main__":
    main()
