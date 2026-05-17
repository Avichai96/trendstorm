"""Integration test: KnowledgePendingEvent → KnowledgeWorker → Mongo + ChromaDB.

Requires `make up` (Mongo + Kafka + Redis + MinIO + Chroma).
Uses Ollama for embeddings if available; falls back to Gemini API key.

Flow:
    1. Upload a text document to MinIO.
    2. Spawn KnowledgeWorker with the real embedding provider.
    3. Publish KnowledgePendingEvent to Kafka.
    4. Poll Mongo until Chunk documents appear.
    5. Verify ChromaDB has vectors, KnowledgeCompletedEvent was published.
    6. Verify idempotency: second publish → skipped=True in completed event.

Skip semantics: if any infrastructure client fails to connect, the test is
skipped (not failed). Run `make up` before running integration tests.
"""
from __future__ import annotations

import asyncio
import contextlib

import pytest
from aiokafka import AIOKafkaConsumer

from trendstorm.agents.knowledge.chunker import ParentChildChunker
from trendstorm.agents.knowledge.pipeline import KnowledgePipeline, _collection_name
from trendstorm.domain.llm.errors import LLMPermanentError
from trendstorm.infrastructure.blob.minio_client import MinioClient
from trendstorm.infrastructure.blob.uri import text_key, to_s3_uri
from trendstorm.infrastructure.kafka.producer import KafkaProducerClient
from trendstorm.infrastructure.mongo.client import MongoClient
from trendstorm.infrastructure.mongo.repositories import (
    IdempotencyRepository,
    MongoChunkRepository,
)
from trendstorm.infrastructure.vectors.chroma_store import ChromaVectorStore
from trendstorm.orchestration.events import (
    KnowledgeCompletedEvent,
    KnowledgeDocRef,
    KnowledgePendingEvent,
)
from trendstorm.orchestration.topics import Topic
from trendstorm.orchestration.workers.knowledge_worker import KnowledgeWorker
from trendstorm.shared.config import get_settings
from trendstorm.shared.ids import new_id

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_ARTICLE_TEXT = (
    "Artificial intelligence is changing the world. "
    "Machine learning models process vast amounts of data. "
    "Natural language processing lets computers understand speech. "
    "Computer vision can identify objects with high accuracy. "
    "These advances create new opportunities across many industries. "
    "The pace of innovation continues to accelerate year after year. "
    "Researchers around the globe are pushing the boundaries of what is possible."
)


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()


@pytest.fixture
async def infra():
    """Connected infrastructure + running KnowledgeWorker. Skips if stack not up."""
    settings = get_settings()

    mongo = MongoClient(settings.mongo)
    minio = MinioClient(settings.blob)
    chroma = ChromaVectorStore(settings.vector)
    producer = KafkaProducerClient(settings.kafka)

    try:
        await asyncio.gather(
            mongo.connect(),
            minio.connect(),
            chroma.connect(),
            producer.start(),
        )
    except Exception as exc:
        pytest.skip(f"Infrastructure not available: {exc}")

    # Resolve embedding provider: prefer Ollama (local, free), fall back to Gemini.
    embedding_provider = None
    try:
        from trendstorm.infrastructure.llm.ollama import OllamaEmbeddingProvider

        ep = OllamaEmbeddingProvider(
            host=settings.llm.ollama_base_url,
            model=settings.llm.ollama_embedding_model,
        )
        await ep.embed_batch(["probe"])
        embedding_provider = ep
    except (LLMPermanentError, Exception):
        gemini_key = settings.gemini.api_key.get_secret_value()
        if not gemini_key:
            await asyncio.gather(
                mongo.close(), minio.close(), chroma.close(), producer.stop()
            )
            pytest.skip("No embedding provider available (Ollama down, no GEMINI__API_KEY)")
        from trendstorm.infrastructure.llm.gemini import GeminiEmbeddingProvider

        embedding_provider = GeminiEmbeddingProvider(api_key=gemini_key)

    chunk_repo = MongoChunkRepository(mongo)
    idem = IdempotencyRepository(mongo)

    pipeline = KnowledgePipeline(
        chunker=ParentChildChunker(parent_size_tokens=100, child_size_tokens=50),
        embedding_provider=embedding_provider,
        chunk_repo=chunk_repo,
        vector_store=chroma,
        minio=minio,
    )

    worker = KnowledgeWorker(
        kafka_settings=settings.kafka,
        pipeline=pipeline,
        idempotency=idem,
        producer=producer,
    )
    await worker.start()
    worker_task = asyncio.create_task(worker.run())

    yield {
        "mongo": mongo,
        "minio": minio,
        "chroma": chroma,
        "chunk_repo": chunk_repo,
        "producer": producer,
        "settings": settings,
        "embedding_provider": embedding_provider,
    }

    await worker.stop()
    worker_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await worker_task
    await asyncio.gather(
        mongo.close(), minio.close(), chroma.close(), producer.stop()
    )


async def _publish_knowledge_pending(
    producer: KafkaProducerClient,
    *,
    tenant_id: str,
    job_id: str,
    document_refs: list[KnowledgeDocRef],
    attempt: int = 1,
) -> None:
    event = KnowledgePendingEvent(
        correlation_id=new_id(),
        tenant_id=tenant_id,
        job_id=job_id,
        document_refs=document_refs,
        attempt=attempt,
    )
    await producer.producer.send_and_wait(
        Topic.KNOWLEDGE_PENDING.value,
        value=event.model_dump_json().encode(),
        key=job_id.encode(),
    )


async def _wait_for_chunks(
    chunk_repo: MongoChunkRepository,
    tenant_id: str,
    document_id: str,
    *,
    min_count: int = 1,
    time_limit: float = 30.0,
) -> list:
    deadline = asyncio.get_event_loop().time() + time_limit
    while asyncio.get_event_loop().time() < deadline:
        chunks = await chunk_repo.list_by_document(tenant_id, document_id)
        if len(chunks) >= min_count:
            return chunks
        await asyncio.sleep(0.5)
    raise AssertionError(
        f"Expected {min_count} Chunk(s) for doc {document_id} within {time_limit}s"
    )


async def _wait_for_vector_ids(
    chunk_repo: MongoChunkRepository,
    tenant_id: str,
    document_id: str,
    *,
    time_limit: float = 30.0,
) -> list:
    """Poll until all child chunks for a document have vector_id populated.

    bulk_insert writes chunks to Mongo before the embedding + Chroma upsert
    steps complete, so _wait_for_chunks can return while vector_id is still
    None. This helper waits for the full pipeline to finish.
    """
    deadline = asyncio.get_event_loop().time() + time_limit
    while asyncio.get_event_loop().time() < deadline:
        chunks = await chunk_repo.list_by_document(tenant_id, document_id)
        children = [c for c in chunks if c.parent_chunk_id is not None]
        if children and all(c.vector_id is not None for c in children):
            return chunks
        await asyncio.sleep(0.5)
    raise AssertionError(
        f"Child chunks for doc {document_id} never got vector_ids within {time_limit}s"
    )


async def _consume_knowledge_completed(
    settings,
    tenant_id: str,
    job_id: str,
    *,
    time_limit: float = 30.0,
) -> KnowledgeCompletedEvent:
    consumer = AIOKafkaConsumer(
        Topic.KNOWLEDGE_COMPLETED.value,
        bootstrap_servers=settings.kafka.bootstrap_servers,
        group_id=f"test-knowledge-{new_id()}",
        auto_offset_reset="earliest",
        enable_auto_commit=True,
    )
    await consumer.start()
    try:
        deadline = asyncio.get_event_loop().time() + time_limit
        while asyncio.get_event_loop().time() < deadline:
            batch = await asyncio.wait_for(
                consumer.getmany(timeout_ms=500, max_records=20), timeout=2.0
            )
            for _tp, records in batch.items():
                for rec in records:
                    try:
                        evt = KnowledgeCompletedEvent.model_validate_json(rec.value)
                    except Exception:
                        continue
                    if evt.tenant_id == tenant_id and evt.job_id == job_id:
                        return evt
    finally:
        await consumer.stop()
    raise AssertionError(
        f"KnowledgeCompletedEvent not found for job {job_id} within {time_limit}s"
    )


async def test_knowledge_creates_chunks_and_vectors(infra: dict) -> None:
    """Worker chunks text, writes to Mongo + ChromaDB, publishes completed event."""
    settings = infra["settings"]
    tenant_id = new_id()
    job_id = new_id()
    doc_id = new_id()
    category_id = new_id()
    source_id = new_id()

    # Upload text to MinIO
    key = text_key(tenant_id, job_id, doc_id)
    await infra["minio"].upload(
        settings.blob.bucket_raw, key, _ARTICLE_TEXT.encode(), content_type="text/plain"
    )
    blob_uri = to_s3_uri(settings.blob.bucket_raw, key)

    # Publish KnowledgePendingEvent
    await _publish_knowledge_pending(
        infra["producer"],
        tenant_id=tenant_id,
        job_id=job_id,
        document_refs=[
            KnowledgeDocRef(
                document_id=doc_id,
                blob_uri_text=blob_uri,
                category_id=category_id,
                source_id=source_id,
            )
        ],
    )

    # Wait until all child chunks have vector_id populated. Using
    # _wait_for_vector_ids (not _wait_for_chunks) because bulk_insert writes
    # chunks to Mongo before the embedding + Chroma upsert steps complete;
    # reading too early would see vector_id=None on freshly-inserted rows.
    chunks = await _wait_for_vector_ids(infra["chunk_repo"], tenant_id, doc_id)
    assert len(chunks) >= 2

    children = [c for c in chunks if c.parent_chunk_id is not None]
    assert len(children) >= 1
    for child in children:
        assert child.vector_id is not None
        assert child.embedding_model is not None

    # Wait for KnowledgeCompletedEvent
    event = await _consume_knowledge_completed(
        settings, tenant_id=tenant_id, job_id=job_id
    )
    assert event.job_id == job_id
    assert len(event.failed_document_ids) == 0
    assert len(event.document_results) == 1
    assert event.document_results[0].document_id == doc_id
    assert event.document_results[0].n_chunks >= 2
    assert event.document_results[0].skipped is False

    # Verify vectors in ChromaDB
    model_id = infra["embedding_provider"].model_id
    collection = _collection_name(tenant_id, model_id)
    child_ids = [c.id for c in children]
    hits = await infra["chroma"].query(
        collection,
        query_embedding=[0.1] * infra["embedding_provider"].dimensions,
        n_results=len(child_ids),
        where={"document_id": {"$eq": doc_id}},
    )
    assert len(hits) == len(child_ids)


async def test_knowledge_idempotency(infra: dict) -> None:
    """A second KnowledgePendingEvent for the same document is skipped."""
    settings = infra["settings"]
    tenant_id = new_id()
    job_id_1 = new_id()
    job_id_2 = new_id()
    doc_id = new_id()

    key = text_key(tenant_id, job_id_1, doc_id)
    await infra["minio"].upload(
        settings.blob.bucket_raw, key, _ARTICLE_TEXT.encode(), content_type="text/plain"
    )
    blob_uri = to_s3_uri(settings.blob.bucket_raw, key)

    ref = KnowledgeDocRef(
        document_id=doc_id,
        blob_uri_text=blob_uri,
        category_id=new_id(),
        source_id=new_id(),
    )

    # First run: creates chunks
    await _publish_knowledge_pending(
        infra["producer"], tenant_id=tenant_id, job_id=job_id_1, document_refs=[ref]
    )
    event1 = await _consume_knowledge_completed(
        settings, tenant_id=tenant_id, job_id=job_id_1
    )
    assert event1.document_results[0].skipped is False
    n_first = event1.document_results[0].n_chunks
    assert n_first > 0

    # Second run: should be skipped (document already chunked)
    await _publish_knowledge_pending(
        infra["producer"], tenant_id=tenant_id, job_id=job_id_2, document_refs=[ref]
    )
    event2 = await _consume_knowledge_completed(
        settings, tenant_id=tenant_id, job_id=job_id_2
    )
    assert event2.document_results[0].skipped is True
    assert event2.document_results[0].n_chunks == 0
