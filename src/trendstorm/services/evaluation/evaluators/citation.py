"""CitationLookupEvaluator — deterministic citation accuracy scoring.

Does NOT use an LLM judge. Instead:
    1. For each citation in the analysis, look up the chunk_id in Mongo.
    2. Compute cosine similarity between the excerpt text and the chunk text
       using the embedding provider.
    3. Score = (citations that exist AND are similar enough) / total_citations.

Why deterministic?
    Citation accuracy is a lookup problem, not an inference problem. An LLM
    judge adds variance and cost without improving accuracy for this specific
    check. The embedding similarity threshold (default 0.65 cosine) catches
    paraphrasing while rejecting hallucinated excerpts.

Edge cases:
    - Analysis with no citations: score=1.0, passed=True (vacuously correct).
    - Citation chunk_id not found in Mongo: that citation scores 0.0.
    - Embedding call fails: the entire evaluator raises (caller decides retry).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opentelemetry import trace

from trendstorm.domain.evaluation.models import DimensionScore, EvalDimension, EvaluationResult
from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    from trendstorm.domain.analyses.models import Analysis
    from trendstorm.domain.evaluation.models import GoldenExample
    from trendstorm.domain.llm.providers import EmbeddingProvider

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)

_DEFAULT_SIMILARITY_THRESHOLD = 0.65


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two dense vectors. Returns value in [-1, 1]."""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(dot / (norm_a * norm_b))


class CitationLookupEvaluator:
    """Deterministic citation accuracy evaluator.

    Satisfies the Evaluator Protocol structurally.

    Args:
        embedding_provider:   EmbeddingProvider for computing excerpt↔chunk similarity.
        chunk_repo:           MongoChunkRepository (duck-typed: async get_by_id(id) ->
                              Chunk | None). Optional — when None, the evaluator falls
                              back to the GoldenExample.chunks list if one is provided.
                              When both are None and citations exist, they score 0.0.
        similarity_threshold: cosine similarity floor for "similar enough" (default 0.65).

    """

    def __init__(
        self,
        embedding_provider: EmbeddingProvider,
        chunk_repo: object | None = None,
        *,
        similarity_threshold: float = _DEFAULT_SIMILARITY_THRESHOLD,
    ) -> None:
        self._embed = embedding_provider
        self._repo = chunk_repo
        self._threshold = similarity_threshold

    @property
    def dimension(self) -> EvalDimension:
        return EvalDimension.CITATION_ACCURACY

    async def evaluate(
        self,
        analysis: Analysis,
        *,
        example: GoldenExample | None = None,
    ) -> EvaluationResult:
        with tracer.start_as_current_span("eval.citation_lookup") as span:
            span.set_attribute("eval.n_citations", len(analysis.citations))

            if not analysis.citations:
                # Vacuously correct — no citations to check.
                score = 1.0
                span.set_attribute("eval.citation.score", score)
                return _build_result(analysis, score, passed=True)

            valid = 0
            total = len(analysis.citations)

            for citation in analysis.citations:
                chunk = await self._lookup_chunk(citation.chunk_id, example=example)
                if chunk is None:
                    logger.debug("eval.citation.chunk_not_found", chunk_id=citation.chunk_id)
                    continue

                sim = await self._similarity(citation.excerpt, chunk.text)
                if sim >= self._threshold:
                    valid += 1
                else:
                    logger.debug(
                        "eval.citation.low_similarity",
                        chunk_id=citation.chunk_id,
                        similarity=sim,
                        threshold=self._threshold,
                    )

            score = valid / total
            passed = score >= _DEFAULT_SIMILARITY_THRESHOLD
            span.set_attribute("eval.citation.score", score)
            span.set_attribute("eval.citation.valid", valid)
            span.set_attribute("eval.citation.total", total)

            return _build_result(analysis, score, passed=passed)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _lookup_chunk(
        self,
        chunk_id: str,
        *,
        example: GoldenExample | None = None,
    ) -> Any:
        # Golden example chunks take priority (in-memory, no I/O).
        # Returns GoldenChunk or Chunk duck type (both have .text); typed Any
        # because chunk_repo is duck-typed as object to avoid domain coupling.
        if example is not None:
            for gc in example.chunks:
                if gc.chunk_id == chunk_id:
                    return gc  # GoldenChunk has .text — same duck type as Chunk
        if self._repo is not None:
            try:
                return await self._repo.get_by_id(chunk_id)  # type: ignore[attr-defined]  # duck-typed repo
            except Exception as exc:
                logger.warning("eval.citation.lookup_error", chunk_id=chunk_id, error=str(exc))
        return None

    async def _similarity(self, excerpt: str, chunk_text: str) -> float:
        result = await self._embed.embed_batch([excerpt, chunk_text], task_type="query")
        if len(result.vectors) < 2:
            return 0.0
        return max(-1.0, min(1.0, _cosine_similarity(result.vectors[0], result.vectors[1])))


def _build_result(analysis: Analysis, score: float, *, passed: bool) -> EvaluationResult:
    ds = DimensionScore(
        dimension=EvalDimension.CITATION_ACCURACY,
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
