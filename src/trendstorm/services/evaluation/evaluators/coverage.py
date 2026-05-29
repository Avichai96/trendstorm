"""GoldenCoverageEvaluator — embedding-similarity recall against expected insights.

Deterministic (no LLM judge). Algorithm:
    For each ExpectedInsight in example.expected_analysis.insights:
        Find the best-matching Insight in the actual analysis by embedding
        similarity between claim texts.
        If max_similarity >= match_threshold → the expected insight is covered.
    recall = covered_required / total_required   (required=True insights only)
    score  = covered_all / total_all             (includes optional insights)

Why embedding similarity over exact match?
    Analyst phrasing varies per run; the expected insight text is the curator's
    wording, not the Analyst's. Embedding similarity captures semantic equivalence
    across paraphrases. A threshold of 0.70 cosine is intentionally loose —
    false negatives (missed real coverage) matter more than false positives here.

Requires a GoldenExample with expected_analysis. If example is None or has no
expected_analysis, raises ValueError — callers must skip this evaluator on
production samples.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opentelemetry import trace

from trendstorm.domain.evaluation.models import DimensionScore, EvalDimension, EvaluationResult
from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    from trendstorm.domain.analyses.models import Analysis
    from trendstorm.domain.evaluation.models import GoldenExample
    from trendstorm.domain.llm.providers import EmbeddingProvider

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)

_DEFAULT_MATCH_THRESHOLD = 0.70


class GoldenCoverageEvaluator:
    """Coverage evaluator against expected insights in a GoldenExample.

    Satisfies the Evaluator Protocol structurally.

    Args:
        embedding_provider: used to embed claim texts for similarity matching.
        match_threshold:    minimum cosine similarity to count a match (default 0.70).

    """

    def __init__(
        self,
        embedding_provider: EmbeddingProvider,
        *,
        match_threshold: float = _DEFAULT_MATCH_THRESHOLD,
    ) -> None:
        self._embed = embedding_provider
        self._threshold = match_threshold

    @property
    def dimension(self) -> EvalDimension:
        return EvalDimension.COVERAGE

    async def evaluate(
        self,
        analysis: Analysis,
        *,
        example: GoldenExample | None = None,
    ) -> EvaluationResult:
        if example is None or example.expected_analysis is None:
            raise ValueError(
                "GoldenCoverageEvaluator requires a GoldenExample with expected_analysis. "
                "Skip this evaluator for production samples."
            )

        expected = example.expected_analysis
        if not expected.insights:
            score = 1.0
            return _build_result(analysis, score, passed=True)

        with tracer.start_as_current_span("eval.coverage") as span:
            # Collect all actual claim texts for batch embedding.
            actual_claims = [i.claim for i in analysis.insights]
            expected_claims = [ei.claim for ei in expected.insights]

            all_claims = actual_claims + expected_claims
            if not all_claims:
                return _build_result(analysis, 1.0, passed=True)

            embed_result = await self._embed.embed_batch(all_claims, task_type="query")
            vectors = embed_result.vectors

            n_actual = len(actual_claims)
            actual_vecs = vectors[:n_actual]
            expected_vecs = vectors[n_actual:]

            covered_required = 0
            covered_all = 0
            total_required = sum(1 for ei in expected.insights if ei.required)
            total_all = len(expected.insights)

            for expected_insight, expected_vec in zip(
                expected.insights, expected_vecs, strict=False
            ):
                best_sim = 0.0
                for actual_vec in actual_vecs:
                    sim = _cosine_similarity(expected_vec, actual_vec)
                    if sim > best_sim:
                        best_sim = sim

                covered = best_sim >= self._threshold
                if covered:
                    covered_all += 1
                    if expected_insight.required:
                        covered_required += 1
                else:
                    logger.debug(
                        "eval.coverage.insight_not_covered",
                        expected_claim=expected_insight.claim[:80],
                        best_similarity=round(best_sim, 3),
                        required=expected_insight.required,
                    )

            # Score = fraction of ALL expected insights covered.
            score = covered_all / total_all if total_all else 1.0
            # Pass = all required insights covered.
            passed = (covered_required == total_required) if total_required else True

            span.set_attribute("eval.coverage.score", score)
            span.set_attribute("eval.coverage.covered_required", covered_required)
            span.set_attribute("eval.coverage.total_required", total_required)

            return _build_result(analysis, score, passed=passed)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(dot / (norm_a * norm_b))


def _build_result(
    analysis: Analysis,
    score: float,
    *,
    passed: bool,
) -> EvaluationResult:
    ds = DimensionScore(
        dimension=EvalDimension.COVERAGE,
        score=score,
        passed=passed,
    )
    return EvaluationResult(
        tenant_id=analysis.tenant_id,
        analysis_id=analysis.id,
        job_id=analysis.job_id,
        dimension_scores=[ds],
        aggregate_score=score,
    )
