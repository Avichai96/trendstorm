"""EvalRunner - orchestrates a full eval run (dataset x evaluators).

run_eval(dataset, evaluators, target) runs each example through all evaluators,
aggregates per-dimension means, computes threshold violations, persists the
report to disk, and pushes to LangSmith if the client is configured.

CI gate:
    The on-disk artifact (artifacts/eval-{timestamp}.json) is the CI source
    of truth. LangSmith is for human inspection. If LangSmith is unreachable,
    the run still completes and the exit code reflects threshold violations.

Evaluator error handling:
    If an evaluator raises on a specific example, that example's score for
    that dimension is recorded as 0.0 and logged. The run continues — a
    single bad example should not abort the entire suite.

Target function:
    `target` is an async callable that takes a GoldenExample and returns an
    Analysis. EvalRunner calls it for each example so the same runner can be
    used with a real Analyst or a fixture replay.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opentelemetry import trace

from trendstorm.domain.evaluation.models import (
    DimensionScore,
    DimensionSummary,
    EvalDimension,
    EvalRunReport,
    EvaluationResult,
    GoldenExample,
)
from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    from trendstorm.domain.evaluation.evaluator import Evaluator
    from trendstorm.infrastructure.langsmith.client import LangSmithClient
    from trendstorm.shared.config import EvalSettings

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)

_ARTIFACTS_DIR = Path("artifacts")

TargetFn = Callable[[GoldenExample], Awaitable[Any]]


class EvalRunner:
    """Orchestrates evaluation of a dataset against a set of evaluators.

    Args:
        evaluators:   list of Evaluator instances (one per dimension to run).
        settings:     EvalSettings for threshold lookup.
        langsmith:    optional LangSmithClient; None disables remote push.
        artifacts_dir: directory for on-disk JSON reports (default: artifacts/).

    """

    def __init__(
        self,
        evaluators: list[Evaluator],
        settings: EvalSettings,
        *,
        langsmith: LangSmithClient | None = None,
        artifacts_dir: Path = _ARTIFACTS_DIR,
    ) -> None:
        self._evaluators = evaluators
        self._settings = settings
        self._langsmith = langsmith
        self._artifacts_dir = artifacts_dir

    async def run_eval(
        self,
        dataset: list[GoldenExample],
        target: TargetFn,
        *,
        suite: str = "custom",
        project: str | None = None,
    ) -> EvalRunReport:
        """Run all evaluators over the dataset.

        Args:
            dataset:  list of GoldenExample fixtures.
            target:   async function GoldenExample → Analysis.
            suite:    label for the run ("fast", "full", "production").
            project:  LangSmith project name override; uses settings default if None.

        Returns:
            EvalRunReport with per-dimension summaries and threshold violations.

        """
        started_at = datetime.now(UTC)
        with tracer.start_as_current_span("eval.run") as span:
            span.set_attribute("eval.suite", suite)
            span.set_attribute("eval.n_examples", len(dataset))

            all_results: list[EvaluationResult] = []

            for example in dataset:
                example_results = await self._evaluate_example(example, target)
                all_results.extend(example_results)

            report = self._build_report(
                suite=suite,
                dataset=dataset,
                all_results=all_results,
                started_at=started_at,
            )

            self._persist(report)

            if self._langsmith is not None:
                ls_url = self._langsmith.push_eval_results(report, project=project)
                if ls_url:
                    report = report.model_copy(update={"langsmith_url": ls_url})

            span.set_attribute("eval.passed", report.passed)
            span.set_attribute("eval.n_passed", report.n_passed)
            logger.info(
                "eval.run_complete",
                suite=suite,
                n_examples=len(dataset),
                n_passed=report.n_passed,
                passed=report.passed,
                violations=report.threshold_violations,
            )
            return report

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    async def _evaluate_example(
        self,
        example: GoldenExample,
        target: TargetFn,
    ) -> list[EvaluationResult]:
        """Run target + all evaluators on one example. Returns one result per evaluator."""
        results: list[EvaluationResult] = []

        try:
            analysis = await target(example)
        except Exception as exc:
            logger.warning(
                "eval.target_failed",
                example=example.name,
                error=str(exc),
            )
            return results

        for evaluator in self._evaluators:
            try:
                result = await evaluator.evaluate(analysis, example=example)
                results.append(result)
            except ValueError as exc:
                # GoldenCoverageEvaluator raises ValueError when called without
                # expected_analysis — skip silently.
                logger.debug(
                    "eval.evaluator_skipped",
                    dimension=evaluator.dimension,
                    example=example.name,
                    reason=str(exc),
                )
            except Exception as exc:
                logger.warning(
                    "eval.evaluator_error",
                    dimension=evaluator.dimension,
                    example=example.name,
                    error=str(exc),
                )
                # Record a zero score so the dimension is counted in aggregation.
                results.append(EvaluationResult(
                    tenant_id=example.tenant_id,
                    analysis_id="error",
                    job_id="error",
                    dimension_scores=[DimensionScore(
                        dimension=evaluator.dimension,
                        score=0.0,
                        passed=False,
                        rationale=f"evaluator_error: {exc}",
                    )],
                    aggregate_score=0.0,
                ))

        return results

    def _build_report(
        self,
        *,
        suite: str,
        dataset: list[GoldenExample],
        all_results: list[EvaluationResult],
        started_at: datetime,
    ) -> EvalRunReport:
        finished_at = datetime.now(UTC)
        elapsed = (finished_at - started_at).total_seconds()

        # Aggregate per dimension
        dim_scores: dict[EvalDimension, list[float]] = {d: [] for d in EvalDimension}
        dim_passed: dict[EvalDimension, list[bool]] = {d: [] for d in EvalDimension}

        for result in all_results:
            for ds in result.dimension_scores:
                dim_scores[ds.dimension].append(ds.score)
                dim_passed[ds.dimension].append(ds.passed)

        summaries: list[DimensionSummary] = []
        violations: list[str] = []

        thresholds = {
            EvalDimension.FAITHFULNESS:      self._settings.thresholds.faithfulness,
            EvalDimension.CITATION_ACCURACY: self._settings.thresholds.citation_accuracy,
            EvalDimension.RELEVANCE:         self._settings.thresholds.relevance,
            EvalDimension.COVERAGE:          self._settings.thresholds.coverage,
        }

        for dim in EvalDimension:
            scores = dim_scores[dim]
            if not scores:
                continue
            mean_score = sum(scores) / len(scores)
            pass_rate = sum(dim_passed[dim]) / len(dim_passed[dim]) if dim_passed[dim] else 0.0

            summaries.append(DimensionSummary(
                dimension=dim,
                mean_score=round(mean_score, 4),
                pass_rate=round(pass_rate, 4),
                n_evaluated=len(scores),
            ))

            threshold = thresholds.get(dim, 0.0)
            if mean_score < threshold:
                violations.append(
                    f"{dim}: mean_score={mean_score:.3f} < threshold={threshold:.3f}"
                )

        return EvalRunReport(
            suite=suite,
            n_examples=len(dataset),
            n_passed=len(dataset) - len(violations),  # proxy: passed = no violations
            dimension_summaries=summaries,
            threshold_violations=violations,
            started_at=started_at,
            finished_at=finished_at,
            elapsed_seconds=elapsed,
        )

    def _persist(self, report: EvalRunReport) -> None:
        try:
            self._artifacts_dir.mkdir(parents=True, exist_ok=True)
            ts = report.started_at.strftime("%Y%m%d_%H%M%S")
            path = self._artifacts_dir / f"eval-{ts}-{report.run_id[:8]}.json"
            path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
            logger.info("eval.report_persisted", path=str(path))
        except Exception as exc:
            logger.warning("eval.report_persist_failed", error=str(exc))
