"""Evaluator Protocol — single entry point for any evaluation dimension.

One Evaluator per dimension. The EvalRunner composes a list of Evaluators
and runs them against each GoldenExample (or production Analysis).

Protocol is @runtime_checkable so callers can verify implementations at
startup without a full type-checker run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from trendstorm.domain.analyses.models import Analysis
    from trendstorm.domain.evaluation.models import EvalDimension, EvaluationResult, GoldenExample


@runtime_checkable
class Evaluator(Protocol):
    """Scores one dimension of an Analysis.

    Implementations must be stateless per call — they can hold injected
    dependencies (repos, embedding providers, panel clients) but must not
    accumulate state across evaluate() calls.
    """

    @property
    def dimension(self) -> EvalDimension:
        """The rubric dimension this evaluator scores."""
        ...

    async def evaluate(
        self,
        analysis: Analysis,
        *,
        example: GoldenExample | None = None,
    ) -> EvaluationResult:
        """Score the analysis on this evaluator's dimension.

        Args:
            analysis: the Analysis to evaluate.
            example:  optional GoldenExample providing chunk corpus and
                      expected_analysis for COVERAGE evaluation.
                      None is acceptable for non-coverage evaluators running
                      on production samples.

        Returns:
            EvaluationResult with at least one DimensionScore for self.dimension.
            The result's tenant_id, analysis_id, and job_id must be populated
            from the analysis fields.

        Raises:
            EvaluationError if the evaluator cannot produce a result
            (e.g. panel quorum failure, embedding provider unreachable).

        """
        ...
