"""Unit tests for KnowledgePipeline using injected fakes.

No real I/O: MinIO, Mongo, ChromaDB, and the embedding provider are all fake.
"""
from __future__ import annotations

from typing import Any

import pytest

from trendstorm.agents.knowledge.chunker import ParentChildChunker
from trendstorm.agents.knowledge.pipeline import (
    KnowledgePipeline,
    KnowledgeResult,
    _collection_name,
)
from trendstorm.domain.chunks.models import Chunk
from trendstorm.domain.llm.models import EmbeddingBatchResult

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeMinio:
    def __init__(self, text: str = "Hello world. Second sentence. Third sentence.") -> None:
        self._text = text
        self.downloads: list[tuple[str, str]] = []

    async def download(self, bucket: str, key: str) -> bytes:
        self.downloads.append((bucket, key))
        return self._text.encode("utf-8")


class _FakeChunkRepo:
    def __init__(self, pre_existing: list[Chunk] | None = None) -> None:
        self.inserted: list[Chunk] = []
        self.vector_id_calls: list[tuple[str, str, str, str]] = []
        self._existing = pre_existing or []

    async def list_by_document(
        self,
        tenant_id: str,
        document_id: str,
        *,
        embedding_model: str | None = None,
    ) -> list[Chunk]:
        if embedding_model is not None:
            return [c for c in self._existing if c.embedding_model == embedding_model]
        return self._existing

    async def bulk_insert(self, chunks: list[Chunk]) -> int:
        self.inserted.extend(chunks)
        return len(chunks)

    async def set_vector_id(
        self, tenant_id: str, chunk_id: str, vector_id: str, embedding_model: str
    ) -> None:
        self.vector_id_calls.append((tenant_id, chunk_id, vector_id, embedding_model))

    async def get(self, tenant_id: str, chunk_id: str) -> Chunk | None:
        return None

    async def get_many(self, tenant_id: str, chunk_ids: list[str]) -> list[Chunk]:
        return []


class _FakeEmbeddingProvider:
    def __init__(self, n_dims: int = 4, max_batch: int = 10) -> None:
        self._n_dims = n_dims
        self._max_batch = max_batch
        self.batches_called: list[list[str]] = []

    @property
    def model_id(self) -> str:
        return "fake.embed-v1"

    @property
    def dimensions(self) -> int:
        return self._n_dims

    @property
    def max_batch_size(self) -> int:
        return self._max_batch

    @property
    def max_input_tokens(self) -> int:
        return 512

    async def embed_batch(self, texts: list[str]) -> EmbeddingBatchResult:
        self.batches_called.append(list(texts))
        return EmbeddingBatchResult(
            vectors=[[0.1] * self._n_dims for _ in texts],
            input_tokens=len(texts) * 3,
            model_id=self.model_id,
        )


class _FakeVectorStore:
    def __init__(self) -> None:
        self.upserts: list[dict[str, Any]] = []

    async def health_check(self) -> bool:
        return True

    async def upsert(
        self,
        collection: str,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: list[dict[str, Any]],
    ) -> None:
        self.upserts.append(
            {
                "collection": collection,
                "ids": list(ids),
                "n_vectors": len(ids),
                "n_documents": len(documents),
                "metadatas": list(metadatas),
            }
        )

    async def query(
        self,
        collection: str,
        query_embedding: list[float],
        n_results: int,
        where: dict[str, Any] | None = None,
    ) -> list[Any]:
        return []

    async def delete_by_filter(self, collection: str, where: dict[str, Any]) -> None:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pipeline(
    text: str = "Hello world. Second sentence. Third sentence. Fourth sentence.",
    pre_existing: list[Chunk] | None = None,
    max_batch: int = 10,
) -> tuple[KnowledgePipeline, _FakeMinio, _FakeChunkRepo, _FakeEmbeddingProvider, _FakeVectorStore]:
    minio = _FakeMinio(text)
    repo = _FakeChunkRepo(pre_existing)
    embed = _FakeEmbeddingProvider(max_batch=max_batch)
    store = _FakeVectorStore()
    chunker = ParentChildChunker(parent_size_tokens=20, child_size_tokens=10, parent_overlap_sentences=1)
    pipeline = KnowledgePipeline(
        chunker=chunker,
        embedding_provider=embed,
        chunk_repo=repo,
        vector_store=store,
        minio=minio,
    )
    return pipeline, minio, repo, embed, store


async def _run(text: str = "Hello world. Second sentence. Third sentence.") -> tuple[KnowledgeResult, _FakeChunkRepo, _FakeEmbeddingProvider, _FakeVectorStore]:
    pipeline, _, repo, embed, store = _make_pipeline(text)
    result = await pipeline.process_document(
        document_id="doc-001",
        blob_uri_text="s3://test-bucket/tenant/job/doc/text.txt",
        tenant_id="01HX00000000000000000000",
        job_id="job-001",
        category_id="cat-001",
        source_id="src-001",
    )
    return result, repo, embed, store


# ---------------------------------------------------------------------------
# Tests: collection name helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCollectionName:
    def test_basic_format(self) -> None:
        name = _collection_name("01HX12345678ABCDEF", "gemini.text-embedding-004")
        assert name == "chunks__01hx1234__gemini_text_embedding_004"

    def test_tenant_truncated_to_8(self) -> None:
        name = _collection_name("ABCDEFGHIJKLMNOP", "openai.text-embedding-3-small")
        assert name.startswith("chunks__abcdefgh__")

    def test_dots_replaced(self) -> None:
        name = _collection_name("01234567", "provider.model.v1")
        assert "." not in name

    def test_hyphens_replaced(self) -> None:
        name = _collection_name("01234567", "provider.model-v1")
        assert "-" not in name


# ---------------------------------------------------------------------------
# Tests: happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestKnowledgePipelineHappyPath:
    async def test_result_not_skipped(self) -> None:
        result, _, _, _ = await _run()
        assert result.skipped is False

    async def test_chunks_created(self) -> None:
        result, _, _, _ = await _run()
        assert result.n_chunks_created > 0

    async def test_vectors_upserted(self) -> None:
        result, _, _, _ = await _run()
        assert result.n_vectors_upserted > 0

    async def test_children_are_subset_of_chunks(self) -> None:
        result, repo, _, _ = await _run()
        parents = [c for c in repo.inserted if c.parent_chunk_id is None]
        children = [c for c in repo.inserted if c.parent_chunk_id is not None]
        assert len(parents) + len(children) == result.n_chunks_created
        assert len(children) == result.n_vectors_upserted

    async def test_parents_have_no_vector_id_initially(self) -> None:
        _, repo, _, _ = await _run()
        for chunk in repo.inserted:
            if chunk.parent_chunk_id is None:
                assert chunk.vector_id is None

    async def test_children_have_embedding_model_set(self) -> None:
        _, repo, _, _ = await _run()
        for chunk in repo.inserted:
            if chunk.parent_chunk_id is not None:
                assert chunk.embedding_model == "fake.embed-v1"

    async def test_vector_store_upserted_once(self) -> None:
        _, _, _, store = await _run()
        assert len(store.upserts) == 1

    async def test_upserted_ids_count_matches_children(self) -> None:
        result, _, _, store = await _run()
        assert store.upserts[0]["n_vectors"] == result.n_vectors_upserted

    async def test_set_vector_id_called_for_each_child(self) -> None:
        result, repo, _, _ = await _run()
        assert len(repo.vector_id_calls) == result.n_vectors_upserted

    async def test_vector_id_equals_chunk_id(self) -> None:
        _, repo, _, _ = await _run()
        for _tenant, chunk_id, vector_id, _model in repo.vector_id_calls:
            assert vector_id == chunk_id

    async def test_minio_download_called_with_correct_bucket_and_key(self) -> None:
        pipeline, minio, _, _, _ = _make_pipeline()
        await pipeline.process_document(
            document_id="d1",
            blob_uri_text="s3://my-bucket/path/to/text.txt",
            tenant_id="t1",
            job_id="j1",
            category_id="c1",
            source_id="s1",
        )
        assert minio.downloads == [("my-bucket", "path/to/text.txt")]

    async def test_metadata_includes_required_keys(self) -> None:
        pipeline, _, _, _, store = _make_pipeline()
        await pipeline.process_document(
            document_id="d1",
            blob_uri_text="s3://b/k",
            tenant_id="tenant-xyz",
            job_id="job-xyz",
            category_id="cat-xyz",
            source_id="src-xyz",
        )
        for meta in store.upserts[0]["metadatas"]:
            assert meta["tenant_id"] == "tenant-xyz"
            assert meta["category_id"] == "cat-xyz"
            assert meta["document_id"] == "d1"
            assert meta["source_id"] == "src-xyz"

    async def test_collection_name_uses_tenant_and_model(self) -> None:
        pipeline, _, _, _, store = _make_pipeline()
        await pipeline.process_document(
            document_id="d1",
            blob_uri_text="s3://b/k",
            tenant_id="01HX12345678XXXX",
            job_id="j1",
            category_id="c1",
            source_id="s1",
        )
        col = store.upserts[0]["collection"]
        assert col.startswith("chunks__01hx1234__")
        assert "fake" in col


# ---------------------------------------------------------------------------
# Tests: idempotency
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestKnowledgePipelineIdempotency:
    async def test_skips_when_chunks_exist(self) -> None:
        # Pre-populate the repo with an existing child chunk for this document,
        # embedded under the same model the fake provider reports ("fake.embed-v1").
        # The idempotency check filters by embedding_model, so this must match.
        existing = [
            Chunk(
                tenant_id="t1",
                job_id="j1",
                category_id="c1",
                document_id="doc-001",
                source_id="s1",
                position=0,
                text="old chunk",
                embedding_model="fake.embed-v1",
            )
        ]
        pipeline, minio, _repo, embed, store = _make_pipeline(pre_existing=existing)
        result = await pipeline.process_document(
            document_id="doc-001",
            blob_uri_text="s3://b/k",
            tenant_id="t1",
            job_id="j1",
            category_id="c1",
            source_id="s1",
        )
        assert result.skipped is True
        assert result.n_chunks_created == 0
        assert result.n_vectors_upserted == 0
        # MinIO was not called
        assert minio.downloads == []
        # Embed was not called
        assert embed.batches_called == []
        # Vector store was not touched
        assert store.upserts == []

    async def test_returns_document_id(self) -> None:
        result, _, _, _ = await _run()
        assert result.document_id == "doc-001"


# ---------------------------------------------------------------------------
# Tests: edge cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestKnowledgePipelineEdgeCases:
    async def test_empty_text_returns_zero_counts(self) -> None:
        pipeline, _, _, _, store = _make_pipeline(text="   ")
        result = await pipeline.process_document(
            document_id="d1",
            blob_uri_text="s3://b/k",
            tenant_id="t1",
            job_id="j1",
            category_id="c1",
            source_id="s1",
        )
        assert result.n_chunks_created == 0
        assert result.n_vectors_upserted == 0
        assert result.skipped is False
        assert store.upserts == []

    async def test_batching_respects_max_batch_size(self) -> None:
        # Use small batch size to force multiple embed calls
        # Generate text with many sentences to ensure many children
        text = " ".join(f"Sentence number {i}." for i in range(30))
        pipeline, _, _, embed, _ = _make_pipeline(text=text, max_batch=3)
        await pipeline.process_document(
            document_id="d1",
            blob_uri_text="s3://b/k",
            tenant_id="t1",
            job_id="j1",
            category_id="c1",
            source_id="s1",
        )
        # Every batch should have at most max_batch_size texts
        for batch in embed.batches_called:
            assert len(batch) <= 3

    async def test_all_chunks_have_document_id(self) -> None:
        _, repo, _, _ = await _run()
        for chunk in repo.inserted:
            assert chunk.document_id == "doc-001"

    async def test_all_chunks_have_tenant_id(self) -> None:
        _, repo, _, _ = await _run()
        for chunk in repo.inserted:
            assert chunk.tenant_id == "01HX00000000000000000000"

    async def test_child_parent_references_valid(self) -> None:
        """Every child's parent_chunk_id points to an existing parent chunk."""
        _, repo, _, _ = await _run()
        parent_ids = {c.id for c in repo.inserted if c.parent_chunk_id is None}
        for chunk in repo.inserted:
            if chunk.parent_chunk_id is not None:
                assert chunk.parent_chunk_id in parent_ids
