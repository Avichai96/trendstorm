"""Unit tests for CitationLookupEvaluator.

Pure: no Mongo, no real embedding provider. Uses fake implementations
to test the scoring logic in isolation.
"""
from __future__ import annotations

import math

import pytest

from trendstorm.domain.evaluation.models import EvalDimension, GoldenChunk, GoldenExample
from trendstorm.domain.llm.models import EmbeddingBatchResult
from trendstorm.services.evaluation.evaluators.citation import (
    CitationLookupEvaluator,
    _cosine_similarity,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeEmbedProvider:
    """Returns controlled vectors for (excerpt, chunk_text) pairs."""

    def __init__(self, vectors: list[list[float]]) -> None:
        self._vectors = vectors
        self.call_count = 0

    @property
    def model_id(self) -> str:
        return "fake-embed"

    @property
    def dimensions(self) -> int:
        return len(self._vectors[0]) if self._vectors else 0

    @property
    def max_batch_size(self) -> int:
        return 100

    @property
    def max_input_tokens(self) -> int:
        return 8192

    async def embed_batch(self, texts: list[str], task_type: str = "document") -> EmbeddingBatchResult:
        self.call_count += 1
        return EmbeddingBatchResult(
            model_id=self.model_id,
            vectors=self._vectors[: len(texts)],
            input_tokens=len(texts) * 10,
        )


def _identical_embed(dim: int = 4) -> _FakeEmbedProvider:
    """Returns identical vectors — cosine similarity = 1.0."""
    v = [1.0] * dim
    return _FakeEmbedProvider([v, v])


def _orthogonal_embed(dim: int = 4) -> _FakeEmbedProvider:
    """Returns orthogonal vectors — cosine similarity = 0.0."""
    v1 = [1.0, 0.0, 0.0, 0.0]
    v2 = [0.0, 1.0, 0.0, 0.0]
    return _FakeEmbedProvider([v1, v2])


def _make_analysis(
    citations: list[dict],
    tenant_id: str = "t1",
    analysis_id: str = "a1",
    job_id: str = "j1",
):
    from trendstorm.domain.analyses.models import Analysis, Citation

    cit_objs = [
        Citation(
            chunk_id=c["chunk_id"],
            document_id=c.get("document_id", "doc1"),
            source_id=c.get("source_id", "src1"),
            excerpt=c.get("excerpt", "excerpt text"),
            url=c.get("url", None),
        )
        for c in citations
    ]
    return Analysis(
        id=analysis_id,
        tenant_id=tenant_id,
        job_id=job_id,
        category_id="cat1",
        summary="summary",
        insights=[],
        citations=cit_objs,
    )


def _golden_example_with_chunks(chunks: list[tuple[str, str]]) -> GoldenExample:
    """Build a GoldenExample with the given (chunk_id, text) pairs."""
    gc = [
        GoldenChunk(
            chunk_id=cid,
            document_id="doc1",
            source_id="src1",
            text=text,
        )
        for cid, text in chunks
    ]
    return GoldenExample(
        name="test",
        category_name="Test Category",
        chunks=gc,
    )


# ---------------------------------------------------------------------------
# _cosine_similarity pure function tests
# ---------------------------------------------------------------------------

class TestCosineSimilarity:
    def test_identical(self):
        v = [1.0, 2.0, 3.0]
        assert _cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert _cosine_similarity(a, b) == pytest.approx(0.0)

    def test_opposite(self):
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        assert _cosine_similarity(a, b) == pytest.approx(-1.0)

    def test_zero_vector(self):
        a = [0.0, 0.0]
        b = [1.0, 0.0]
        assert _cosine_similarity(a, b) == 0.0

    def test_both_zero(self):
        a = [0.0, 0.0]
        b = [0.0, 0.0]
        assert _cosine_similarity(a, b) == 0.0

    def test_partial_similarity(self):
        a = [1.0, 1.0, 0.0]
        b = [1.0, 0.0, 0.0]
        # cos θ = 1 / sqrt(2) ≈ 0.707
        assert _cosine_similarity(a, b) == pytest.approx(1 / math.sqrt(2), rel=1e-3)


# ---------------------------------------------------------------------------
# CitationLookupEvaluator — no citations
# ---------------------------------------------------------------------------

class TestNoCitations:
    @pytest.mark.asyncio
    async def test_vacuously_correct(self):
        embed = _identical_embed()
        evaluator = CitationLookupEvaluator(embed)
        analysis = _make_analysis(citations=[])

        result = await evaluator.evaluate(analysis)
        ds = result.dimension_scores[0]

        assert ds.dimension == EvalDimension.CITATION_ACCURACY
        assert ds.score == pytest.approx(1.0)
        assert ds.passed is True
        # No embedding calls needed for empty citations
        assert embed.call_count == 0


# ---------------------------------------------------------------------------
# CitationLookupEvaluator — golden example chunk fallback
# ---------------------------------------------------------------------------

class TestGoldenChunkFallback:
    @pytest.mark.asyncio
    async def test_found_and_similar(self):
        embed = _identical_embed()  # similarity = 1.0 ≥ threshold
        evaluator = CitationLookupEvaluator(embed)

        example = _golden_example_with_chunks([("c1", "chunk text here")])
        analysis = _make_analysis(citations=[{"chunk_id": "c1", "excerpt": "chunk text here"}])

        result = await evaluator.evaluate(analysis, example=example)
        ds = result.dimension_scores[0]

        assert ds.score == pytest.approx(1.0)
        assert ds.passed is True

    @pytest.mark.asyncio
    async def test_not_found_scores_zero(self):
        embed = _identical_embed()
        evaluator = CitationLookupEvaluator(embed)

        example = _golden_example_with_chunks([("c2", "some text")])
        # citation references chunk_id "c_missing" which is not in example
        analysis = _make_analysis(citations=[{"chunk_id": "c_missing", "excerpt": "text"}])

        result = await evaluator.evaluate(analysis, example=example)
        ds = result.dimension_scores[0]

        assert ds.score == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_partial_score(self):
        embed = _identical_embed()  # all found chunks will match
        evaluator = CitationLookupEvaluator(embed)

        example = _golden_example_with_chunks([("c1", "text one"), ("c2", "text two")])
        # c1 found, c3 not found
        analysis = _make_analysis(
            citations=[
                {"chunk_id": "c1", "excerpt": "text one"},
                {"chunk_id": "c3", "excerpt": "text three"},
            ]
        )

        result = await evaluator.evaluate(analysis, example=example)
        ds = result.dimension_scores[0]

        # 1 valid out of 2 total = 0.5
        assert ds.score == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# CitationLookupEvaluator — low similarity (orthogonal vectors)
# ---------------------------------------------------------------------------

class TestLowSimilarity:
    @pytest.mark.asyncio
    async def test_orthogonal_fails_threshold(self):
        embed = _orthogonal_embed()  # similarity = 0.0 < 0.65 threshold
        evaluator = CitationLookupEvaluator(embed)

        example = _golden_example_with_chunks([("c1", "chunk text")])
        analysis = _make_analysis(
            citations=[{"chunk_id": "c1", "excerpt": "completely different"}]
        )

        result = await evaluator.evaluate(analysis, example=example)
        ds = result.dimension_scores[0]

        assert ds.score == pytest.approx(0.0)
        assert ds.passed is False


# ---------------------------------------------------------------------------
# CitationLookupEvaluator — chunk_repo duck type
# ---------------------------------------------------------------------------

class TestChunkRepoFallback:
    @pytest.mark.asyncio
    async def test_chunk_repo_used_when_no_example(self):
        embed = _identical_embed()

        class _FakeChunk:
            text = "chunk text from mongo"

        class _FakeRepo:
            async def get_by_id(self, chunk_id: str) -> _FakeChunk | None:
                if chunk_id == "c_from_mongo":
                    return _FakeChunk()
                return None

        evaluator = CitationLookupEvaluator(embed, chunk_repo=_FakeRepo())
        analysis = _make_analysis(
            citations=[{"chunk_id": "c_from_mongo", "excerpt": "chunk text from mongo"}]
        )

        result = await evaluator.evaluate(analysis)
        ds = result.dimension_scores[0]

        assert ds.score == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_example_chunks_take_priority_over_repo(self):
        """Golden example chunks are checked before the Mongo repo."""
        embed = _identical_embed()

        class _FakeRepo:
            called = False

            async def get_by_id(self, chunk_id: str):
                _FakeRepo.called = True
                return None

        example = _golden_example_with_chunks([("c1", "chunk text in golden")])
        evaluator = CitationLookupEvaluator(embed, chunk_repo=_FakeRepo())
        analysis = _make_analysis(citations=[{"chunk_id": "c1", "excerpt": "match"}])

        result = await evaluator.evaluate(analysis, example=example)
        # The repo should NOT have been called because example had the chunk
        assert not _FakeRepo.called
        ds = result.dimension_scores[0]
        assert ds.score == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_repo_error_counts_as_not_found(self):
        embed = _identical_embed()

        class _ErrorRepo:
            async def get_by_id(self, chunk_id: str):
                raise RuntimeError("DB unavailable")

        evaluator = CitationLookupEvaluator(embed, chunk_repo=_ErrorRepo())
        analysis = _make_analysis(citations=[{"chunk_id": "c1", "excerpt": "text"}])

        result = await evaluator.evaluate(analysis)
        ds = result.dimension_scores[0]
        # repo error → chunk not found → score 0
        assert ds.score == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# EvaluationResult structure
# ---------------------------------------------------------------------------

class TestResultStructure:
    @pytest.mark.asyncio
    async def test_result_has_correct_fields(self):
        embed = _identical_embed()
        evaluator = CitationLookupEvaluator(embed)
        example = _golden_example_with_chunks([("c1", "text")])
        analysis = _make_analysis(
            citations=[{"chunk_id": "c1", "excerpt": "text"}],
            tenant_id="tenant-a",
            analysis_id="analysis-x",
            job_id="job-y",
        )

        result = await evaluator.evaluate(analysis, example=example)

        assert result.tenant_id == "tenant-a"
        assert result.analysis_id == "analysis-x"
        assert result.job_id == "job-y"
        assert len(result.dimension_scores) == 1
        assert result.dimension_scores[0].dimension == EvalDimension.CITATION_ACCURACY
        assert result.aggregate_score >= 0.0
