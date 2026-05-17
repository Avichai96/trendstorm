"""Integration test: KnowledgePipeline against real Mongo, MinIO, and ChromaDB.

Requires `make up`. Skipped gracefully if any service is unavailable.
Uses Ollama for embeddings (runs in the compose stack; no API key needed).
If Ollama is also unavailable, falls back to the Gemini API key from settings.

Run manually:
    uv run pytest tests/integration/test_knowledge_pipeline.py -m integration -s
"""
from __future__ import annotations

import pytest

from trendstorm.agents.knowledge.chunker import ParentChildChunker
from trendstorm.agents.knowledge.pipeline import KnowledgePipeline
from trendstorm.domain.llm.errors import LLMPermanentError
from trendstorm.infrastructure.blob.minio_client import MinioClient
from trendstorm.infrastructure.mongo.client import MongoClient
from trendstorm.infrastructure.mongo.repositories import MongoChunkRepository
from trendstorm.infrastructure.vectors.chroma_store import ChromaVectorStore
from trendstorm.shared.config import get_settings
from trendstorm.shared.ids import new_id

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_ARTICLE_TEXT = """
Artificial intelligence is transforming every industry it touches.
Machine learning models can now process vast amounts of data in real time.
Natural language processing enables computers to understand human speech.
Computer vision systems can identify objects with superhuman accuracy.
Reinforcement learning allows agents to master complex games and simulations.
These advances are creating new opportunities across healthcare, finance, and logistics.
The pace of innovation continues to accelerate year after year.
"""


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()


@pytest.fixture
async def infra():
    """Connected infrastructure clients. Skips if stack is not running."""
    settings = get_settings()

    mongo = MongoClient(settings.mongo)
    minio = MinioClient(settings.blob)
    chroma = ChromaVectorStore(settings.vector)

    try:
        await mongo.connect()
        await minio.connect()
        await chroma.connect()
    except Exception as exc:
        pytest.skip(f"Infrastructure not available: {exc}")

    # Try Ollama first (free, no key), fall back to Gemini if key is present.
    embedding_provider = None
    try:
        from trendstorm.infrastructure.llm.ollama import OllamaEmbeddingProvider

        ep = OllamaEmbeddingProvider(
            host=settings.llm.ollama_base_url,
            model=settings.llm.ollama_embedding_model,
        )
        await ep.embed_batch(["probe"])  # connectivity check
        embedding_provider = ep
    except (LLMPermanentError, Exception):
        gemini_key = settings.gemini.api_key.get_secret_value()
        if gemini_key:
            from trendstorm.infrastructure.llm.gemini import GeminiEmbeddingProvider

            embedding_provider = GeminiEmbeddingProvider(
                api_key=gemini_key, model=settings.gemini.embedding_model
            )
        else:
            await mongo.close()
            await minio.close()
            await chroma.close()
            pytest.skip("No embedding provider available (Ollama down, no GEMINI__API_KEY)")

    chunk_repo = MongoChunkRepository(mongo)

    yield {
        "mongo": mongo,
        "minio": minio,
        "chroma": chroma,
        "chunk_repo": chunk_repo,
        "embedding_provider": embedding_provider,
        "settings": settings,
    }

    await chroma.close()
    await minio.close()
    await mongo.close()


def _make_pipeline(infra: dict) -> KnowledgePipeline:
    return KnowledgePipeline(
        chunker=ParentChildChunker(
            parent_size_tokens=100,
            child_size_tokens=50,
            parent_overlap_sentences=1,
        ),
        embedding_provider=infra["embedding_provider"],
        chunk_repo=infra["chunk_repo"],
        vector_store=infra["chroma"],
        minio=infra["minio"],
    )


async def test_pipeline_creates_chunks_and_vectors(infra: dict) -> None:
    """Full pipeline: upload text → chunk → embed → Mongo + Chroma."""
    settings = infra["settings"]
    tenant_id = new_id()
    job_id = new_id()
    doc_id = new_id()
    category_id = new_id()
    source_id = new_id()

    # Upload text to MinIO
    from trendstorm.infrastructure.blob.uri import text_key, to_s3_uri

    key = text_key(tenant_id, job_id, doc_id)
    await infra["minio"].upload(
        settings.blob.bucket_raw, key, _ARTICLE_TEXT.encode(), content_type="text/plain"
    )
    blob_uri = to_s3_uri(settings.blob.bucket_raw, key)

    pipeline = _make_pipeline(infra)
    result = await pipeline.process_document(
        document_id=doc_id,
        blob_uri_text=blob_uri,
        tenant_id=tenant_id,
        job_id=job_id,
        category_id=category_id,
        source_id=source_id,
    )

    assert result.skipped is False
    assert result.n_chunks_created > 0
    assert result.n_vectors_upserted > 0

    # Verify chunks in Mongo
    chunks = await infra["chunk_repo"].list_by_document(tenant_id, doc_id)
    assert len(chunks) == result.n_chunks_created

    # Verify children have vector_ids set
    children = [c for c in chunks if c.parent_chunk_id is not None]
    assert len(children) == result.n_vectors_upserted
    for child in children:
        assert child.vector_id == child.id
        assert child.embedding_model is not None


async def test_pipeline_idempotency(infra: dict) -> None:
    """Running the pipeline twice for the same document is a no-op."""
    settings = infra["settings"]
    tenant_id = new_id()
    job_id = new_id()
    doc_id = new_id()

    from trendstorm.infrastructure.blob.uri import text_key, to_s3_uri

    key = text_key(tenant_id, job_id, doc_id)
    await infra["minio"].upload(
        settings.blob.bucket_raw, key, _ARTICLE_TEXT.encode(), content_type="text/plain"
    )
    blob_uri = to_s3_uri(settings.blob.bucket_raw, key)

    pipeline = _make_pipeline(infra)

    result1 = await pipeline.process_document(
        document_id=doc_id,
        blob_uri_text=blob_uri,
        tenant_id=tenant_id,
        job_id=job_id,
        category_id=new_id(),
        source_id=new_id(),
    )
    assert result1.skipped is False

    result2 = await pipeline.process_document(
        document_id=doc_id,
        blob_uri_text=blob_uri,
        tenant_id=tenant_id,
        job_id=job_id,
        category_id=new_id(),
        source_id=new_id(),
    )
    assert result2.skipped is True
    assert result2.n_chunks_created == 0
    assert result2.n_vectors_upserted == 0
