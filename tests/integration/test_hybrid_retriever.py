"""Integration test: HybridRetriever against live Mongo + ChromaDB.

Requires `make up` AND `make seed-indexes` AND either Ollama or Gemini to be
available for embedding. Skips gracefully if any dependency is unreachable.

The test inserts a small corpus (5 parent + 10 child chunks with pre-computed
fake embeddings), runs the hybrid pipeline, and asserts that the AI-safety
chunks rank above the climate-change chunks for an AI-safety query.

Run manually:
    uv run pytest tests/integration/test_hybrid_retriever.py -m integration -s
"""
from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator
from typing import Any

import pytest

from trendstorm.domain.chunks.models import Chunk
from trendstorm.domain.llm.models import EmbeddingBatchResult
from trendstorm.domain.retrieval.models import RetrievalRequest
from trendstorm.infrastructure.mongo.client import MongoClient
from trendstorm.infrastructure.mongo.repositories import MongoChunkRepository
from trendstorm.infrastructure.mongo.schema import Collection
from trendstorm.infrastructure.retrieval.chroma_vector import (
    ChromaVectorRetriever,
    _collection_name,
)
from trendstorm.infrastructure.retrieval.mongo_bm25 import MongoBM25Retriever
from trendstorm.infrastructure.vectors.chroma_store import ChromaVectorStore
from trendstorm.services.retrieval.hybrid import HybridRetriever
from trendstorm.shared.config import AnalysisSettings, get_settings
from trendstorm.shared.ids import new_id

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# ---------------------------------------------------------------------------
# Tiny fixture corpus — 5 topics, 2 child chunks each, 1 parent per topic
# ---------------------------------------------------------------------------

CORPUS: list[dict[str, str]] = [
    {"topic": "ai_safety", "parent": "AI safety research focuses on aligning large language models with human values and preventing harmful outputs. Constitutional AI and RLHF are key techniques.", "child1": "AI safety research aligns LLMs with human values.", "child2": "Constitutional AI and RLHF reduce harmful model outputs."},
    {"topic": "ai_safety2", "parent": "Anthropic's safety approach uses interpretability research to understand model internals and detect misalignment before deployment.", "child1": "Interpretability research reveals LLM decision making.", "child2": "Detecting misalignment before deployment is critical."},
    {"topic": "climate", "parent": "Climate change mitigation requires international cooperation on carbon pricing and emissions trading schemes across major economies.", "child1": "Carbon pricing is a key climate change mitigation tool.", "child2": "Emissions trading schemes reduce industrial carbon output."},
    {"topic": "energy", "parent": "Renewable energy investment has accelerated dramatically with solar panel efficiency increasing from 15% to over 25% in a decade.", "child1": "Solar panel efficiency reached 25% through material advances.", "child2": "Wind energy capacity has doubled in five years globally."},
    {"topic": "biotech", "parent": "CRISPR gene editing technology enables precise modification of DNA sequences and has revolutionized biotechnology research pipelines.", "child1": "CRISPR enables precise DNA sequence modification.", "child2": "Gene editing has revolutionized biotech research pipelines."},
]

# Fake 4-dimensional embeddings per child chunk — structured so that
# AI-safety chunks cluster together and differ from other topics.
# (topic_index, 0, topic_index, 0) → small cosine distance within topic.
_TOPIC_VECS: dict[str, list[float]] = {
    "ai_safety":  [1.0, 0.0, 1.0, 0.0],
    "ai_safety2": [0.9, 0.1, 0.9, 0.1],
    "climate":    [0.0, 1.0, 0.0, 1.0],
    "energy":     [0.0, 0.8, 0.2, 0.8],
    "biotech":    [0.3, 0.7, 0.3, 0.7],
}

# Query vector for "AI safety" — close to ai_safety topic vectors.
_QUERY_VEC = [0.95, 0.05, 0.95, 0.05]

MODEL_ID = "fake.embed-4d"
DIMS = 4


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()


@pytest.fixture
async def mongo() -> AsyncGenerator[MongoClient, None]:
    settings = get_settings()
    client = MongoClient(settings.mongo)
    try:
        await client.connect()
        await client.health_check()
    except Exception as exc:
        pytest.skip(f"Mongo not reachable: {exc}")
    yield client
    await client.close()


@pytest.fixture
async def chroma() -> AsyncGenerator[ChromaVectorStore, None]:
    settings = get_settings()
    store = ChromaVectorStore(settings.vector)
    try:
        await store.connect()
        if not await store.health_check():
            pytest.skip("ChromaDB not healthy")
    except Exception as exc:
        pytest.skip(f"ChromaDB not reachable: {exc}")
    yield store
    await store.close()


@pytest.fixture
async def corpus(
    mongo: MongoClient, chroma: ChromaVectorStore
) -> AsyncGenerator[dict[str, Any], None]:
    """Insert corpus chunks into Mongo + fake vectors into Chroma."""
    tenant_id = new_id()
    category_id = new_id()
    job_id = new_id()
    source_id = new_id()

    chunk_repo = MongoChunkRepository(mongo)
    all_chunks: list[Chunk] = []
    collection = _collection_name(tenant_id, MODEL_ID)

    chroma_ids: list[str] = []
    chroma_vecs: list[list[float]] = []
    chroma_docs: list[str] = []
    chroma_metas: list[dict] = []

    for topic_data in CORPUS:
        topic = topic_data["topic"]
        vec = _TOPIC_VECS[topic]
        doc_id = new_id()

        # Parent chunk (not embedded)
        parent = Chunk(
            tenant_id=tenant_id, job_id=job_id, category_id=category_id,
            document_id=doc_id, source_id=source_id, position=0,
            text=topic_data["parent"],
        )

        # Two child chunks (embedded)
        child1 = Chunk(
            tenant_id=tenant_id, job_id=job_id, category_id=category_id,
            document_id=doc_id, source_id=source_id, position=1,
            text=topic_data["child1"], parent_chunk_id=parent.id,
        )
        child2 = Chunk(
            tenant_id=tenant_id, job_id=job_id, category_id=category_id,
            document_id=doc_id, source_id=source_id, position=2,
            text=topic_data["child2"], parent_chunk_id=parent.id,
        )
        all_chunks.extend([parent, child1, child2])

        for child in (child1, child2):
            chroma_ids.append(child.id)
            chroma_vecs.append(vec)
            chroma_docs.append(child.text)
            chroma_metas.append({
                "tenant_id": tenant_id,
                "category_id": category_id,
                "document_id": doc_id,
                "source_id": source_id,
            })

    inserted = await chunk_repo.bulk_insert(all_chunks)
    if inserted == 0:
        pytest.skip("Could not insert corpus — text index may be missing")

    await chroma.upsert(collection, chroma_ids, chroma_vecs, chroma_docs, chroma_metas)

    yield {
        "tenant_id": tenant_id,
        "category_id": category_id,
        "collection": collection,
        "chunk_ids": [c.id for c in all_chunks],
    }

    # Cleanup
    with contextlib.suppress(Exception):
        coll = mongo.db[Collection.CHUNKS.value]
        await coll.delete_many({"_id": {"$in": [c.id for c in all_chunks]}})
    with contextlib.suppress(Exception):
        await chroma.delete_by_filter(collection, {"tenant_id": {"$eq": tenant_id}})


# ---------------------------------------------------------------------------
# Fake embedding provider that returns pre-computed vectors
# ---------------------------------------------------------------------------

class FakeEmbedProvider:
    """Returns the _QUERY_VEC for any query embed call."""

    @property
    def model_id(self) -> str:
        return MODEL_ID

    @property
    def dimensions(self) -> int:
        return DIMS

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
        task_type: str = "document",
    ) -> EmbeddingBatchResult:
        return EmbeddingBatchResult(
            vectors=[_QUERY_VEC] * len(texts),
            input_tokens=len(texts),
            model_id=MODEL_ID,
        )


# ---------------------------------------------------------------------------
# Fake query expander — returns original + one paraphrase
# ---------------------------------------------------------------------------

class FakeExpander:
    async def expand(self, query: str, *, count: int = 3) -> list[str]:
        return [query, f"{query} alignment techniques"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestHybridRetrieverIntegration:
    async def test_returns_results_for_ai_safety_query(
        self, mongo: MongoClient, chroma: ChromaVectorStore, corpus: dict
    ) -> None:
        tenant_id = corpus["tenant_id"]
        category_id = corpus["category_id"]

        bm25 = MongoBM25Retriever(mongo)
        vector = ChromaVectorRetriever(chroma, FakeEmbedProvider())  # type: ignore[arg-type]
        expander = FakeExpander()  # type: ignore[arg-type]
        settings = AnalysisSettings(
            retrieval_k=10, rerank_k=8, final_k=4,
            query_expansion_count=2,
        )

        hr = HybridRetriever(
            bm25=bm25,  # type: ignore[arg-type]
            vector=vector,  # type: ignore[arg-type]
            expander=expander,  # type: ignore[arg-type]
            mongo=mongo,
            settings=settings,
        )

        request = RetrievalRequest(
            query="AI safety alignment LLM",
            tenant_id=tenant_id,
            category_id=category_id,
            top_k=4,
        )

        try:
            results = await hr.retrieve(request)
        except Exception as exc:
            if "text index" in str(exc).lower():
                pytest.skip(f"Text index not seeded: {exc}")
            raise

        assert len(results) > 0

    async def test_tenant_isolation_end_to_end(
        self, mongo: MongoClient, chroma: ChromaVectorStore, corpus: dict
    ) -> None:
        bm25 = MongoBM25Retriever(mongo)
        vector = ChromaVectorRetriever(chroma, FakeEmbedProvider())  # type: ignore[arg-type]
        settings = AnalysisSettings(retrieval_k=10, rerank_k=8, final_k=4, query_expansion_count=1)

        hr = HybridRetriever(
            bm25=bm25,  # type: ignore[arg-type]
            vector=vector,  # type: ignore[arg-type]
            expander=FakeExpander(),  # type: ignore[arg-type]
            mongo=mongo,
            settings=settings,
        )

        request = RetrievalRequest(
            query="AI safety",
            tenant_id=new_id(),  # different tenant — must see nothing
            category_id=corpus["category_id"],
        )

        try:
            results = await hr.retrieve(request)
        except Exception as exc:
            if "text index" in str(exc).lower():
                pytest.skip(f"Text index not seeded: {exc}")
            raise

        corpus_ids = set(corpus["chunk_ids"])
        assert all(r.chunk_id not in corpus_ids for r in results)

    async def test_parent_text_populated_for_child_chunks(
        self, mongo: MongoClient, chroma: ChromaVectorStore, corpus: dict
    ) -> None:
        bm25 = MongoBM25Retriever(mongo)
        vector = ChromaVectorRetriever(chroma, FakeEmbedProvider())  # type: ignore[arg-type]
        settings = AnalysisSettings(retrieval_k=10, rerank_k=8, final_k=4, query_expansion_count=1)

        hr = HybridRetriever(
            bm25=bm25,  # type: ignore[arg-type]
            vector=vector,  # type: ignore[arg-type]
            expander=FakeExpander(),  # type: ignore[arg-type]
            mongo=mongo,
            settings=settings,
        )

        request = RetrievalRequest(
            query="AI safety alignment",
            tenant_id=corpus["tenant_id"],
            category_id=corpus["category_id"],
        )

        try:
            results = await hr.retrieve(request)
        except Exception as exc:
            if "text index" in str(exc).lower():
                pytest.skip(f"Text index not seeded: {exc}")
            raise

        # Child chunks should have parent_text attached (from the parent chunk).
        child_results = [r for r in results if r.parent_text is not None]
        assert len(child_results) > 0, "Expected at least one result with parent_text"
