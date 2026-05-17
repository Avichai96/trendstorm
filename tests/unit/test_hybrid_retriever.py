"""Unit tests for HybridRetriever.

All backends are faked: no Mongo, no Chroma, no Cohere, no LLM.
Tests verify orchestration logic — expansion fan-out, RRF merging,
reranker fallback, parent expansion, backend error isolation.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trendstorm.domain.chunks.models import Chunk
from trendstorm.domain.retrieval.models import RetrievalRequest, RetrievedChunk
from trendstorm.services.retrieval.hybrid import HybridRetriever
from trendstorm.shared.config import AnalysisSettings
from trendstorm.shared.ids import new_id

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _settings(**overrides: object) -> AnalysisSettings:
    defaults: dict[str, object] = {
        "retrieval_k": 50,
        "rerank_k": 10,
        "final_k": 5,
        "query_expansion_count": 2,
        "validator_threshold": 0.75,
        "max_refinement_loops": 2,
    }
    defaults.update(overrides)
    return AnalysisSettings(**defaults)  # type: ignore[arg-type]


def _make_chunk(
    chunk_id: str | None = None,
    score: float = 0.5,
    text: str = "chunk text",
    document_id: str | None = None,
    source_id: str | None = None,
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id or new_id(),
        score=score,
        text=text,
        document_id=document_id or new_id(),
        source_id=source_id or new_id(),
    )


def _fake_retriever(results: list[RetrievedChunk]):
    """Returns a mock that satisfies both BM25Retriever and VectorRetriever protocols."""
    r = MagicMock()
    r.retrieve = AsyncMock(return_value=results)
    return r


def _fake_expander(sub_queries: list[str]):
    e = MagicMock()
    e.expand = AsyncMock(return_value=sub_queries)
    return e


def _fake_reranker(results: list[RetrievedChunk]):
    r = MagicMock()
    r.rerank = AsyncMock(return_value=results)
    return r


def _fake_mongo(chunk_docs: list[Chunk]):
    """Fake MongoClient — MongoChunkRepository.get_many is patched separately."""
    return MagicMock()


def _make_chunk_doc(
    chunk_id: str,
    text: str = "text",
    parent_chunk_id: str | None = None,
    tenant_id: str = "t",
) -> Chunk:
    return Chunk(
        id=chunk_id,
        tenant_id=tenant_id,
        job_id=new_id(),
        category_id=new_id(),
        document_id=new_id(),
        source_id=new_id(),
        position=0,
        text=text,
        parent_chunk_id=parent_chunk_id,
    )


def _make_retriever(chunk_ids: list[str], score: float = 0.5) -> object:
    """Fake retriever that returns chunks for the given IDs."""
    chunks = [_make_chunk(chunk_id=cid, score=score) for cid in chunk_ids]
    return _fake_retriever(chunks)


def _build_hybrid(
    *,
    bm25_chunks: list[str] | None = None,
    vector_chunks: list[str] | None = None,
    sub_queries: list[str] | None = None,
    reranker=None,
    settings: AnalysisSettings | None = None,
    mongo_chunks: list[Chunk] | None = None,
) -> tuple[HybridRetriever, object, object]:
    """Build a HybridRetriever with faked dependencies.

    Returns (retriever, bm25_mock, vector_mock) for assertion access.
    """
    bm25 = _make_retriever(bm25_chunks or [])
    vector = _make_retriever(vector_chunks or [])
    expander = _fake_expander(sub_queries or ["original query"])
    mongo = _fake_mongo(mongo_chunks or [])
    s = settings or _settings()

    retriever = HybridRetriever(
        bm25=bm25,  # type: ignore[arg-type]
        vector=vector,  # type: ignore[arg-type]
        expander=expander,
        mongo=mongo,
        settings=s,
        reranker=reranker,
    )
    return retriever, bm25, vector


def _request(query: str = "test query", tenant_id: str = "t1", category_id: str = "c1") -> RetrievalRequest:
    return RetrievalRequest(query=query, tenant_id=tenant_id, category_id=category_id)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestHybridRetrieverExpansion:
    async def test_expander_called_with_original_query_and_count(self) -> None:
        s = _settings(query_expansion_count=3)
        expander = _fake_expander(["q1", "q2", "q3"])
        bm25 = _fake_retriever([])
        vector = _fake_retriever([])

        hr = HybridRetriever(
            bm25=bm25,  # type: ignore[arg-type]
            vector=vector,  # type: ignore[arg-type]
            expander=expander,
            mongo=_fake_mongo([]),
            settings=s,
        )
        with patch.object(hr._chunk_repo, "get_many", new=AsyncMock(return_value=[])):
            await hr.retrieve(_request())

        expander.expand.assert_called_once()
        call_kwargs = expander.expand.call_args.kwargs
        assert call_kwargs["count"] == 3

    async def test_both_backends_called_for_each_sub_query(self) -> None:
        s = _settings(query_expansion_count=2)
        expander = _fake_expander(["sub-q-1", "sub-q-2"])
        bm25 = _fake_retriever([])
        vector = _fake_retriever([])

        hr = HybridRetriever(
            bm25=bm25,  # type: ignore[arg-type]
            vector=vector,  # type: ignore[arg-type]
            expander=expander,
            mongo=_fake_mongo([]),
            settings=s,
        )
        with patch.object(hr._chunk_repo, "get_many", new=AsyncMock(return_value=[])):
            await hr.retrieve(_request())

        # 2 sub-queries x 2 backends = 4 retrieve calls total
        assert bm25.retrieve.call_count == 2
        assert vector.retrieve.call_count == 2


@pytest.mark.unit
class TestHybridRetrieverRRF:
    async def test_chunks_from_both_backends_merged(self) -> None:
        cid_a, cid_b = new_id(), new_id()
        s = _settings(query_expansion_count=1, rerank_k=10, final_k=5)
        bm25 = _fake_retriever([_make_chunk(chunk_id=cid_a, score=0.9)])
        vector = _fake_retriever([_make_chunk(chunk_id=cid_b, score=0.8)])
        expander = _fake_expander(["q"])

        hr = HybridRetriever(
            bm25=bm25,  # type: ignore[arg-type]
            vector=vector,  # type: ignore[arg-type]
            expander=expander,
            mongo=_fake_mongo([]),
            settings=s,
        )
        with patch.object(hr._chunk_repo, "get_many", new=AsyncMock(return_value=[])):
            results = await hr.retrieve(_request())

        result_ids = {r.chunk_id for r in results}
        assert cid_a in result_ids
        assert cid_b in result_ids

    async def test_chunk_in_both_backends_ranks_higher(self) -> None:
        shared = new_id()
        only_bm25 = new_id()
        only_vector = new_id()
        s = _settings(query_expansion_count=1, rerank_k=10, final_k=5)

        bm25 = _fake_retriever([
            _make_chunk(chunk_id=shared, score=0.5),
            _make_chunk(chunk_id=only_bm25, score=0.4),
        ])
        vector = _fake_retriever([
            _make_chunk(chunk_id=shared, score=0.5),
            _make_chunk(chunk_id=only_vector, score=0.4),
        ])
        expander = _fake_expander(["q"])

        hr = HybridRetriever(
            bm25=bm25,  # type: ignore[arg-type]
            vector=vector,  # type: ignore[arg-type]
            expander=expander,
            mongo=_fake_mongo([]),
            settings=s,
        )
        with patch.object(hr._chunk_repo, "get_many", new=AsyncMock(return_value=[])):
            results = await hr.retrieve(_request())

        assert results[0].chunk_id == shared

    async def test_backend_exception_excluded_from_rrf(self) -> None:
        cid = new_id()
        s = _settings(query_expansion_count=1, rerank_k=10, final_k=5)

        bm25 = MagicMock()
        bm25.retrieve = AsyncMock(side_effect=RuntimeError("bm25 down"))
        vector = _fake_retriever([_make_chunk(chunk_id=cid)])
        expander = _fake_expander(["q"])

        hr = HybridRetriever(
            bm25=bm25,  # type: ignore[arg-type]
            vector=vector,  # type: ignore[arg-type]
            expander=expander,
            mongo=_fake_mongo([]),
            settings=s,
        )
        with patch.object(hr._chunk_repo, "get_many", new=AsyncMock(return_value=[])):
            results = await hr.retrieve(_request())

        # Vector results survive BM25 failure.
        assert len(results) >= 1
        assert results[0].chunk_id == cid

    async def test_all_backends_fail_returns_empty(self) -> None:
        s = _settings(query_expansion_count=1)
        bm25 = MagicMock()
        bm25.retrieve = AsyncMock(side_effect=RuntimeError("down"))
        vector = MagicMock()
        vector.retrieve = AsyncMock(side_effect=RuntimeError("down"))
        expander = _fake_expander(["q"])

        hr = HybridRetriever(
            bm25=bm25,  # type: ignore[arg-type]
            vector=vector,  # type: ignore[arg-type]
            expander=expander,
            mongo=_fake_mongo([]),
            settings=s,
        )
        results = await hr.retrieve(_request())
        assert results == []

    async def test_rerank_k_caps_candidates(self) -> None:
        # 5 distinct chunks, rerank_k=3 → at most 3 passed to reranker
        cids = [new_id() for _ in range(5)]
        s = _settings(query_expansion_count=1, rerank_k=3, final_k=2)
        bm25 = _fake_retriever([_make_chunk(chunk_id=c) for c in cids])
        vector = _fake_retriever([])
        expander = _fake_expander(["q"])

        reranker_chunks = [_make_chunk(chunk_id=cids[0]), _make_chunk(chunk_id=cids[1])]
        reranker = _fake_reranker(reranker_chunks)

        hr = HybridRetriever(
            bm25=bm25,  # type: ignore[arg-type]
            vector=vector,  # type: ignore[arg-type]
            expander=expander,
            mongo=_fake_mongo([]),
            settings=s,
            reranker=reranker,  # type: ignore[arg-type]
        )
        with patch.object(hr._chunk_repo, "get_many", new=AsyncMock(return_value=[])):
            await hr.retrieve(_request())

        reranker.rerank.assert_called_once()
        passed_candidates = reranker.rerank.call_args.args[1]
        assert len(passed_candidates) <= 3


@pytest.mark.unit
class TestHybridRetrieverReranker:
    async def test_reranker_used_when_provided(self) -> None:
        cid = new_id()
        s = _settings(query_expansion_count=1, final_k=1)
        bm25 = _fake_retriever([_make_chunk(chunk_id=cid)])
        vector = _fake_retriever([])
        expander = _fake_expander(["q"])
        reranker = _fake_reranker([_make_chunk(chunk_id=cid, score=0.99)])

        hr = HybridRetriever(
            bm25=bm25,  # type: ignore[arg-type]
            vector=vector,  # type: ignore[arg-type]
            expander=expander,
            mongo=_fake_mongo([]),
            settings=s,
            reranker=reranker,  # type: ignore[arg-type]
        )
        with patch.object(hr._chunk_repo, "get_many", new=AsyncMock(return_value=[])):
            results = await hr.retrieve(_request())

        reranker.rerank.assert_called_once()
        assert results[0].score == pytest.approx(0.99)

    async def test_reranker_failure_falls_back_to_rrf_top_k(self) -> None:
        cids = [new_id() for _ in range(5)]
        s = _settings(query_expansion_count=1, rerank_k=5, final_k=2)
        bm25 = _fake_retriever([_make_chunk(chunk_id=c) for c in cids])
        vector = _fake_retriever([])
        expander = _fake_expander(["q"])

        failing_reranker = MagicMock()
        failing_reranker.rerank = AsyncMock(side_effect=RuntimeError("cohere down"))

        hr = HybridRetriever(
            bm25=bm25,  # type: ignore[arg-type]
            vector=vector,  # type: ignore[arg-type]
            expander=expander,
            mongo=_fake_mongo([]),
            settings=s,
            reranker=failing_reranker,  # type: ignore[arg-type]
        )
        with patch.object(hr._chunk_repo, "get_many", new=AsyncMock(return_value=[])):
            results = await hr.retrieve(_request())

        # Fallback: final_k results from RRF order, not an error
        assert len(results) == 2

    async def test_no_reranker_uses_rrf_top_final_k(self) -> None:
        cids = [new_id() for _ in range(10)]
        s = _settings(query_expansion_count=1, rerank_k=10, final_k=3)
        bm25 = _fake_retriever([_make_chunk(chunk_id=c) for c in cids])
        vector = _fake_retriever([])
        expander = _fake_expander(["q"])

        hr = HybridRetriever(
            bm25=bm25,  # type: ignore[arg-type]
            vector=vector,  # type: ignore[arg-type]
            expander=expander,
            mongo=_fake_mongo([]),
            settings=s,
        )
        with patch.object(hr._chunk_repo, "get_many", new=AsyncMock(return_value=[])):
            results = await hr.retrieve(_request())

        assert len(results) == 3


@pytest.mark.unit
class TestHybridRetrieverParentExpansion:
    async def test_parent_text_attached_for_child_chunks(self) -> None:
        cid = new_id()
        parent_id = new_id()
        s = _settings(query_expansion_count=1, final_k=1)

        bm25 = _fake_retriever([_make_chunk(chunk_id=cid)])
        vector = _fake_retriever([])
        expander = _fake_expander(["q"])

        child_doc = _make_chunk_doc(cid, text="child text", parent_chunk_id=parent_id)
        parent_doc = _make_chunk_doc(parent_id, text="PARENT CONTEXT TEXT")

        hr = HybridRetriever(
            bm25=bm25,  # type: ignore[arg-type]
            vector=vector,  # type: ignore[arg-type]
            expander=expander,
            mongo=_fake_mongo([]),
            settings=s,
        )

        async def fake_get_many(tenant_id: str, chunk_ids: list[str]) -> list[Chunk]:
            docs = {child_doc.id: child_doc, parent_doc.id: parent_doc}
            return [docs[cid] for cid in chunk_ids if cid in docs]

        with patch.object(hr._chunk_repo, "get_many", new=AsyncMock(side_effect=fake_get_many)):
            results = await hr.retrieve(_request())

        assert len(results) == 1
        assert results[0].parent_text == "PARENT CONTEXT TEXT"

    async def test_parent_text_none_when_chunk_is_parent(self) -> None:
        cid = new_id()
        s = _settings(query_expansion_count=1, final_k=1)

        bm25 = _fake_retriever([_make_chunk(chunk_id=cid)])
        vector = _fake_retriever([])
        expander = _fake_expander(["q"])

        # parent_chunk_id=None → this IS a parent chunk
        chunk_doc = _make_chunk_doc(cid, text="parent chunk text", parent_chunk_id=None)

        hr = HybridRetriever(
            bm25=bm25,  # type: ignore[arg-type]
            vector=vector,  # type: ignore[arg-type]
            expander=expander,
            mongo=_fake_mongo([]),
            settings=s,
        )
        with patch.object(hr._chunk_repo, "get_many", new=AsyncMock(return_value=[chunk_doc])):
            results = await hr.retrieve(_request())

        assert results[0].parent_text is None

    async def test_mongo_lookup_missing_chunk_preserves_result(self) -> None:
        # If Mongo doesn't return a doc for a chunk_id, the result still appears
        # but with parent_text=None (no crash).
        cid = new_id()
        s = _settings(query_expansion_count=1, final_k=1)

        bm25 = _fake_retriever([_make_chunk(chunk_id=cid)])
        vector = _fake_retriever([])
        expander = _fake_expander(["q"])

        hr = HybridRetriever(
            bm25=bm25,  # type: ignore[arg-type]
            vector=vector,  # type: ignore[arg-type]
            expander=expander,
            mongo=_fake_mongo([]),
            settings=s,
        )
        # get_many returns empty — chunk not found in Mongo (shouldn't happen in prod)
        with patch.object(hr._chunk_repo, "get_many", new=AsyncMock(return_value=[])):
            results = await hr.retrieve(_request())

        assert len(results) == 1
        assert results[0].parent_text is None
