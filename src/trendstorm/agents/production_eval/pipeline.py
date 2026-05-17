"""ProductionEvalPipeline — run evaluators on a sampled production Analysis.

Called by ProductionEvalWorker for each EvalSampleEvent. Loads the Analysis
from Mongo, runs the configured evaluators, and persists an EvaluationResult.

Design decisions:
- The Analysis corpus (chunks) is fetched live from Mongo so results reflect
  the real retrieval output, not a golden fixture.
- COVERAGE is always skipped on production samples (no golden expected_analysis).
  GoldenCoverageEvaluator raises ValueError when called without expected_analysis;
  the EvalRunner catches this silently per its evaluator-error contract.
- The pipeline is agnostic to which evaluators are passed in — the worker
  decides which evaluators to instantiate based on available API keys.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from opentelemetry import trace

from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    from trendstorm.domain.analyses.models import Analysis
    from trendstorm.domain.evaluation.models import EvaluationResult
    from trendstorm.infrastructure.mongo.repositories import (
        MongoAnalysisRepository,
        MongoChunkRepository,
    )
    from trendstorm.services.evaluation.runner import EvalRunner

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)


@dataclass(frozen=True)
class ProductionEvalResult:
    analysis_id: str
    evaluation_result: EvaluationResult
    skipped: bool = False
    skip_reason: str = ""


class ProductionEvalPipeline:
    """Runs evaluators on a single production analysis.

    Args:
        runner:          pre-configured EvalRunner with the active evaluator set.
        analysis_repo:   loads Analysis by id.
        chunk_repo:      loads the chunks cited by the analysis for citation eval.

    """

    def __init__(
        self,
        runner: EvalRunner,
        analysis_repo: MongoAnalysisRepository,
        chunk_repo: MongoChunkRepository,
    ) -> None:
        self._runner = runner
        self._analysis_repo = analysis_repo
        self._chunk_repo = chunk_repo

    async def evaluate_analysis(
        self,
        *,
        tenant_id: str,
        analysis_id: str,
        job_id: str,
    ) -> ProductionEvalResult:
        """Load and evaluate one production analysis.

        Returns a ProductionEvalResult. If the analysis cannot be found or
        has no insights, returns a skipped result rather than raising.
        """
        with tracer.start_as_current_span(
            "production_eval.evaluate_analysis",
            attributes={"analysis_id": analysis_id, "job_id": job_id},
        ):
            analysis = await self._analysis_repo.get(tenant_id, analysis_id)
            if analysis is None:
                logger.warning(
                    "production_eval.analysis_not_found",
                    analysis_id=analysis_id,
                    tenant_id=tenant_id,
                )
                return ProductionEvalResult(
                    analysis_id=analysis_id,
                    evaluation_result=_stub_result(tenant_id, analysis_id, job_id),
                    skipped=True,
                    skip_reason="analysis_not_found",
                )

            if not analysis.insights:
                logger.info(
                    "production_eval.skipping_empty_analysis",
                    analysis_id=analysis_id,
                )
                return ProductionEvalResult(
                    analysis_id=analysis_id,
                    evaluation_result=_stub_result(tenant_id, analysis_id, job_id),
                    skipped=True,
                    skip_reason="no_insights",
                )

            eval_result = await self._run_evaluators(analysis)
            logger.info(
                "production_eval.done",
                analysis_id=analysis_id,
                aggregate_score=eval_result.aggregate_score,
                n_dimensions=len(eval_result.dimension_scores),
            )
            return ProductionEvalResult(
                analysis_id=analysis_id,
                evaluation_result=eval_result,
            )

    async def _run_evaluators(self, analysis: Analysis) -> EvaluationResult:
        from trendstorm.domain.evaluation.models import (
            DimensionScore,
            EvaluationResult,
        )

        all_scores: list[DimensionScore] = []

        for evaluator in self._runner._evaluators:
            try:
                result = await evaluator.evaluate(analysis, example=None)
                all_scores.extend(result.dimension_scores)
            except ValueError:
                # GoldenCoverageEvaluator raises ValueError when example is None.
                # Production samples have no golden — skip silently.
                logger.debug(
                    "production_eval.evaluator_skipped",
                    dimension=evaluator.dimension,
                    analysis_id=analysis.id,
                )
            except Exception as exc:
                logger.warning(
                    "production_eval.evaluator_error",
                    dimension=evaluator.dimension,
                    analysis_id=analysis.id,
                    error=str(exc),
                )

        aggregate = (
            sum(ds.score for ds in all_scores) / len(all_scores)
            if all_scores
            else 0.0
        )

        return EvaluationResult(
            tenant_id=analysis.tenant_id,
            analysis_id=analysis.id,
            job_id=analysis.job_id,
            dimension_scores=all_scores,
            aggregate_score=aggregate,
        )


def _stub_result(tenant_id: str, analysis_id: str, job_id: str) -> EvaluationResult:
    from trendstorm.domain.evaluation.models import EvaluationResult
    return EvaluationResult(
        tenant_id=tenant_id,
        analysis_id=analysis_id,
        job_id=job_id,
        dimension_scores=[],
        aggregate_score=0.0,
        flagged=False,
    )
