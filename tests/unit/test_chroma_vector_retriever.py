"""Unit tests for ChromaVectorRetriever.

All I/O is faked: a fake ChromaVectorStore and a fake EmbeddingProvider.
No real Chroma or embedding API calls are made.
"""
from __future__ import annotations

from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock

import pytest

from trendstorm.domain.llm.models import EmbeddingBatchResult
from trendstorm.domain.retrieval.models import RetrievalRequest
from trendstorm.domain.retrieval.protocols import VectorRetriever
from trendstorm.domain.vectors.models import VectorHit
from trendstorm.infrastructure.retrieval.chroma_vector import (
    ChromaVectorRetriever,
    _chroma_where,
    _collection_name,
)
from trendstorm.shared.ids import new_id

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_embed(vectors: list[list[float]], model_id: str = "gemini.text-embedding-004"):
    class FakeEmbedProvider:
        @property
        def model_id(self) -> str:
            return model_id

        @property
        def dimensions(self) -> int:
            return len(vectors[0]) if vectors else 4

        @property
        def max_batch_size(self) -> int:
            return 100

        @property
        def max_input_tokens(self) -> int:
            return 2048

        async def embed_batch(
            self,
            texts: list[str],
            *,
            task_type: Literal["document", "query"] = "document",
        ) -> EmbeddingBatchResult:
            return EmbeddingBatchResult(
                vectors=vectors[: len(texts)],
                input_tokens=len(texts),
                model_id=model_id,
            )

    return FakeEmbedProvider()


def _fake_store(hits: list[VectorHit]) -> Any:
    store = MagicMock()
    store.query = AsyncMock(return_value=hits)
    return store


def _make_hit(
    chunk_id: str,
    score: float,
    text: str,
    document_id: str,
    source_id: str,
) -> VectorHit:
    return VectorHit(
        id=chunk_id,
        score=score,
        metadata={"document_id": document_id, "source_id": source_id},
        document=text,
    )


def _make_request(**kwargs: Any) -> RetrievalRequest:
    defaults: dict[str, Any] = {
        "query": "AI safety research",
        "tenant_id": new_id(),
        "category_id": new_id(),
        "top_k": 5,
    }
    defaults.update(kwargs)
    return RetrievalRequest(**defaults)


# ---------------------------------------------------------------------------
# Unit helpers (pure functions)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCollectionName:
    def test_standard_format(self) -> None:
        name = _collection_name("01HXABCDEFGHIJKLMNOPQRSTU", "gemini.text-embedding-004")
        assert name == "chunks__01hxabcd__gemini_text_embedding_004"

    def test_dots_and_dashes_replaced(self) -> None:
        name = _collection_name("AABBCCDDEE_OTHER", "openai.text-embedding-3-small")
        assert name == "chunks__aabbccdd__openai_text_embedding_3_small"

    def test_tenant_truncated_to_8_chars(self) -> None:
        name = _collection_name("AAABBBCCCDDDEEE", "m.v1")
        assert name.startswith("chunks__aaabbbcc__")


@pytest.mark.unit
class TestChromaWhere:
    def test_structure(self) -> None:
        where = _chroma_where("tenant_x", "cat_y")
        assert "$and" in where
        assert len(where["$and"]) == 2

    def test_contains_both_filters(self) -> None:
        where = _chroma_where("t1", "c1")
        fields = {next(iter(c.keys())) for c in where["$and"]}
        assert "tenant_id" in fields
        assert "category_id" in fields


# ---------------------------------------------------------------------------
# ChromaVectorRetriever behaviour
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestChromaVectorRetriever:
    async def test_satisfies_vector_retriever_protocol(self) -> None:
        retriever = ChromaVectorRetriever(
            vector_store=_fake_store([]),
            embedding_provider=_fake_embed([[0.1, 0.2]]),
        )
        assert isinstance(retriever, VectorRetriever)

    async def test_returns_retrieved_chunks_from_hits(self) -> None:
        doc_id, src_id = new_id(), new_id()
        hits = [
            _make_hit("c1", 0.9, "chunk text one", doc_id, src_id),
            _make_hit("c2", 0.7, "chunk text two", doc_id, src_id),
        ]
        retriever = ChromaVectorRetriever(
            vector_store=_fake_store(hits),
            embedding_provider=_fake_embed([[0.1] * 4]),
        )
        results = await retriever.retrieve(_make_request())
        assert len(results) == 2
        assert results[0].chunk_id == "c1"
        assert results[0].text == "chunk text one"
        assert results[0].score == 0.9
        assert results[0].document_id == doc_id
        assert results[0].source_id == src_id

    async def test_parent_text_and_source_url_are_none(self) -> None:
        hits = [_make_hit("c1", 0.8, "some text", new_id(), new_id())]
        retriever = ChromaVectorRetriever(
            vector_store=_fake_store(hits),
            embedding_provider=_fake_embed([[0.1] * 4]),
        )
        results = await retriever.retrieve(_make_request())
        assert results[0].parent_text is None
        assert results[0].source_url is None

    async def test_hit_with_no_document_is_skipped(self) -> None:
        hit_with_text = _make_hit("c1", 0.9, "has text", new_id(), new_id())
        hit_no_text = VectorHit(
            id="c2", score=0.8, metadata={"document_id": "d", "source_id": "s"}, document=None
        )
        retriever = ChromaVectorRetriever(
            vector_store=_fake_store([hit_with_text, hit_no_text]),
            embedding_provider=_fake_embed([[0.1] * 4]),
        )
        results = await retriever.retrieve(_make_request())
        assert len(results) == 1
        assert results[0].chunk_id == "c1"

    async def test_embed_called_with_task_type_query(self) -> None:
        received: list[str] = []

        class CapturingEmbedder:
            @property
            def model_id(self) -> str:
                return "fake.model"

            @property
            def dimensions(self) -> int:
                return 4

            @property
            def max_batch_size(self) -> int:
                return 100

            @property
            def max_input_tokens(self) -> int:
                return 2048

            async def embed_batch(
                self,
                texts: list[str],
                *,
                task_type: Literal["document", "query"] = "document",
            ) -> EmbeddingBatchResult:
                received.append(task_type)
                return EmbeddingBatchResult(
                    vectors=[[0.1] * 4] * len(texts),
                    input_tokens=1,
                    model_id="fake.model",
                )

        retriever = ChromaVectorRetriever(
            vector_store=_fake_store([]),
            embedding_provider=CapturingEmbedder(),  # type: ignore[arg-type]
        )
        await retriever.retrieve(_make_request())
        assert received == ["query"]

    async def test_empty_hits_returns_empty_list(self) -> None:
        retriever = ChromaVectorRetriever(
            vector_store=_fake_store([]),
            embedding_provider=_fake_embed([[0.1] * 4]),
        )
        results = await retriever.retrieve(_make_request())
        assert results == []

    async def test_store_called_with_correct_top_k(self) -> None:
        store = _fake_store([])
        retriever = ChromaVectorRetriever(
            vector_store=store,
            embedding_provider=_fake_embed([[0.1] * 4]),
        )
        await retriever.retrieve(_make_request(top_k=42))
        store.query.assert_called_once()
        call_args = store.query.call_args
        # n_results is the 3rd positional arg (index 2) in VectorStore.query signature
        n_results = call_args.args[2] if len(call_args.args) > 2 else call_args.kwargs.get("n_results")
        assert n_results == 42

    async def test_store_called_with_tenant_and_category_filter(self) -> None:
        store = _fake_store([])
        tenant_id, category_id = new_id(), new_id()
        retriever = ChromaVectorRetriever(
            vector_store=store,
            embedding_provider=_fake_embed([[0.1] * 4]),
        )
        await retriever.retrieve(_make_request(tenant_id=tenant_id, category_id=category_id))
        where = store.query.call_args[1]["where"]
        # $and filter must reference both tenant_id and category_id
        fields = {next(iter(c.keys())) for c in where["$and"]}
        assert "tenant_id" in fields
        assert "category_id" in fields
