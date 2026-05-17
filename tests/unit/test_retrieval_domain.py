"""Unit tests for domain/retrieval — models and Protocols.

These are pure-function tests: no network, no Mongo, no Chroma.
The integration tests in tests/integration/ exercise the concrete implementations.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from trendstorm.domain.retrieval import (
    BM25Retriever,
    CrossEncoderReranker,
    RetrievalRequest,
    RetrievedChunk,
    Retriever,
    VectorRetriever,
)


@pytest.mark.unit
class TestRetrievalRequest:
    def test_defaults(self) -> None:
        req = RetrievalRequest(query="AI safety", tenant_id="t1", category_id="c1")
        assert req.top_k == 10

    def test_custom_top_k(self) -> None:
        req = RetrievalRequest(query="q", tenant_id="t", category_id="c", top_k=50)
        assert req.top_k == 50

    def test_empty_query_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RetrievalRequest(query="", tenant_id="t", category_id="c")

    def test_top_k_lower_bound(self) -> None:
        with pytest.raises(ValidationError):
            RetrievalRequest(query="q", tenant_id="t", category_id="c", top_k=0)

    def test_top_k_upper_bound(self) -> None:
        with pytest.raises(ValidationError):
            RetrievalRequest(query="q", tenant_id="t", category_id="c", top_k=501)

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            RetrievalRequest(  # type: ignore[call-arg]
                query="q", tenant_id="t", category_id="c", unknown_field="x"
            )

    def test_is_frozen(self) -> None:
        req = RetrievalRequest(query="q", tenant_id="t", category_id="c")
        with pytest.raises(ValidationError):
            req.query = "other"  # type: ignore[misc]


@pytest.mark.unit
class TestRetrievedChunk:
    def _make(self, **kwargs: object) -> RetrievedChunk:
        defaults: dict[str, object] = {
            "chunk_id": "chunk_01",
            "score": 0.85,
            "text": "LLM safety is improving.",
            "document_id": "doc_01",
            "source_id": "src_01",
        }
        defaults.update(kwargs)
        return RetrievedChunk(**defaults)  # type: ignore[arg-type]

    def test_minimal_valid(self) -> None:
        chunk = self._make()
        assert chunk.chunk_id == "chunk_01"
        assert chunk.parent_text is None
        assert chunk.source_url is None

    def test_with_parent_and_url(self) -> None:
        chunk = self._make(
            parent_text="Wider paragraph context for the LLM.",
            source_url="https://example.com/article",
        )
        assert chunk.parent_text is not None
        assert chunk.source_url == "https://example.com/article"

    def test_empty_text_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._make(text="")

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            self._make(unknown="x")  # type: ignore[call-arg]

    def test_score_can_be_negative(self) -> None:
        # Scores are not bounded — BM25 and RRF produce different ranges.
        chunk = self._make(score=-1.0)
        assert chunk.score == -1.0

    def test_score_can_be_zero(self) -> None:
        chunk = self._make(score=0.0)
        assert chunk.score == 0.0


@pytest.mark.unit
class TestRetrieverProtocols:
    """Structural subtyping checks — verifies that the Protocol shapes are consistent."""

    def test_retriever_protocol_is_runtime_checkable(self) -> None:
        # runtime_checkable allows isinstance() — needed in HybridRetriever ctor.
        assert hasattr(Retriever, "__protocol_attrs__") or hasattr(
            Retriever, "_is_protocol"
        )

    def test_bm25_and_vector_are_distinct_protocols(self) -> None:
        assert BM25Retriever is not VectorRetriever
        assert BM25Retriever is not Retriever

    def test_cross_encoder_reranker_is_runtime_checkable(self) -> None:
        assert hasattr(CrossEncoderReranker, "__protocol_attrs__") or hasattr(
            CrossEncoderReranker, "_is_protocol"
        )

    def test_concrete_class_satisfies_retriever_protocol(self) -> None:
        class FakeRetriever:
            async def retrieve(self, request: RetrievalRequest) -> list[RetrievedChunk]:
                return []

        assert isinstance(FakeRetriever(), Retriever)
        assert isinstance(FakeRetriever(), BM25Retriever)
        assert isinstance(FakeRetriever(), VectorRetriever)

    def test_concrete_class_satisfies_reranker_protocol(self) -> None:
        class FakeReranker:
            async def rerank(
                self,
                query: str,
                candidates: list[RetrievedChunk],
                *,
                top_k: int,
            ) -> list[RetrievedChunk]:
                return candidates[:top_k]

        assert isinstance(FakeReranker(), CrossEncoderReranker)

    def test_class_missing_retrieve_does_not_satisfy_protocol(self) -> None:
        class BadRetriever:
            pass

        assert not isinstance(BadRetriever(), Retriever)
