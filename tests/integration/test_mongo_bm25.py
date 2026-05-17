"""Integration test: MongoBM25Retriever against the live Mongo compose service.

Requires `make up` AND `make seed-indexes` (the chunks__text_bm25 index must exist).
Skipped automatically if Mongo is unreachable or the text index is missing.

Run manually:
    uv run pytest tests/integration/test_mongo_bm25.py -m integration -s
"""
from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from trendstorm.domain.chunks.models import Chunk
from trendstorm.domain.retrieval.models import RetrievalRequest
from trendstorm.infrastructure.mongo.client import MongoClient
from trendstorm.infrastructure.mongo.repositories import MongoChunkRepository
from trendstorm.infrastructure.retrieval.mongo_bm25 import MongoBM25Retriever
from trendstorm.shared.config import get_settings
from trendstorm.shared.ids import new_id

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


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
async def corpus(mongo: MongoClient) -> AsyncGenerator[dict[str, str], None]:
    """Insert a small fixture corpus; delete it after the test.

    Returns a dict with the tenant_id, category_id, and job_id used so the
    test can build queries and assertions against known data.
    """
    tenant_id = new_id()
    category_id = new_id()
    job_id = new_id()
    doc_id = new_id()
    source_id = new_id()

    repo = MongoChunkRepository(mongo)
    chunks = [
        Chunk(
            tenant_id=tenant_id,
            job_id=job_id,
            category_id=category_id,
            document_id=doc_id,
            source_id=source_id,
            position=i,
            text=text,
        )
        for i, text in enumerate([
            "Artificial intelligence safety research is crucial for aligning LLM behaviour.",
            "Large language models exhibit emergent capabilities at scale.",
            "Reinforcement learning from human feedback improves instruction following.",
            "Climate change policy requires international cooperation and carbon pricing.",
            "Renewable energy investment is accelerating in solar and wind sectors.",
        ])
    ]

    inserted = await repo.bulk_insert(chunks)
    if inserted == 0:
        pytest.skip("Could not insert test chunks — index may be missing")

    yield {
        "tenant_id": tenant_id,
        "category_id": category_id,
        "job_id": job_id,
        "doc_id": doc_id,
        "source_id": source_id,
        "chunk_ids": [c.id for c in chunks],
    }

    # Cleanup: delete the test chunks by their IDs.
    from trendstorm.infrastructure.mongo.schema import Collection
    try:
        coll = mongo.db[Collection.CHUNKS.value]
        await coll.delete_many({"_id": {"$in": [c.id for c in chunks]}})
    except Exception:
        pass  # best-effort cleanup; test data is ULID-isolated anyway


class TestMongoBM25Retriever:
    async def test_relevant_query_returns_results(
        self, mongo: MongoClient, corpus: dict[str, str]
    ) -> None:
        retriever = MongoBM25Retriever(mongo)
        request = RetrievalRequest(
            query="artificial intelligence safety",
            tenant_id=corpus["tenant_id"],
            category_id=corpus["category_id"],
            top_k=5,
        )
        try:
            results = await retriever.retrieve(request)
        except Exception as exc:
            if "text index" in str(exc).lower() or "no text index" in str(exc).lower():
                pytest.skip(f"Text index not found: {exc}")
            raise

        assert len(results) > 0
        # Top result must be the AI safety chunk (most relevant to the query).
        assert results[0].text is not None
        assert "artificial intelligence" in results[0].text.lower() or \
               "safety" in results[0].text.lower() or \
               "llm" in results[0].text.lower()

    async def test_results_have_correct_provenance(
        self, mongo: MongoClient, corpus: dict[str, str]
    ) -> None:
        retriever = MongoBM25Retriever(mongo)
        request = RetrievalRequest(
            query="language models",
            tenant_id=corpus["tenant_id"],
            category_id=corpus["category_id"],
        )
        try:
            results = await retriever.retrieve(request)
        except Exception as exc:
            if "text index" in str(exc).lower():
                pytest.skip(f"Text index not found: {exc}")
            raise

        assert len(results) > 0
        for r in results:
            assert r.document_id == corpus["doc_id"]
            assert r.source_id == corpus["source_id"]
            assert r.parent_text is None   # not filled at this layer
            assert r.source_url is None    # not filled at this layer
            assert r.score > 0.0

    async def test_scores_ordered_descending(
        self, mongo: MongoClient, corpus: dict[str, str]
    ) -> None:
        retriever = MongoBM25Retriever(mongo)
        request = RetrievalRequest(
            query="reinforcement learning human feedback",
            tenant_id=corpus["tenant_id"],
            category_id=corpus["category_id"],
            top_k=5,
        )
        try:
            results = await retriever.retrieve(request)
        except Exception as exc:
            if "text index" in str(exc).lower():
                pytest.skip(f"Text index not found: {exc}")
            raise

        if len(results) < 2:
            return  # can't check ordering with one result

        for i in range(len(results) - 1):
            assert results[i].score >= results[i + 1].score

    async def test_tenant_isolation(
        self, mongo: MongoClient, corpus: dict[str, str]
    ) -> None:
        retriever = MongoBM25Retriever(mongo)
        other_tenant = new_id()
        request = RetrievalRequest(
            query="artificial intelligence safety",
            tenant_id=other_tenant,
            category_id=corpus["category_id"],
        )
        try:
            results = await retriever.retrieve(request)
        except Exception as exc:
            if "text index" in str(exc).lower():
                pytest.skip(f"Text index not found: {exc}")
            raise

        # A different tenant must see nothing from the corpus.
        chunk_ids = {r.chunk_id for r in results}
        assert chunk_ids.isdisjoint(set(corpus["chunk_ids"]))

    async def test_category_isolation(
        self, mongo: MongoClient, corpus: dict[str, str]
    ) -> None:
        retriever = MongoBM25Retriever(mongo)
        other_category = new_id()
        request = RetrievalRequest(
            query="artificial intelligence",
            tenant_id=corpus["tenant_id"],
            category_id=other_category,
        )
        try:
            results = await retriever.retrieve(request)
        except Exception as exc:
            if "text index" in str(exc).lower():
                pytest.skip(f"Text index not found: {exc}")
            raise

        chunk_ids = {r.chunk_id for r in results}
        assert chunk_ids.isdisjoint(set(corpus["chunk_ids"]))

    async def test_top_k_limit_respected(
        self, mongo: MongoClient, corpus: dict[str, str]
    ) -> None:
        retriever = MongoBM25Retriever(mongo)
        request = RetrievalRequest(
            query="the",   # common word; should match most chunks
            tenant_id=corpus["tenant_id"],
            category_id=corpus["category_id"],
            top_k=2,
        )
        try:
            results = await retriever.retrieve(request)
        except Exception as exc:
            if "text index" in str(exc).lower():
                pytest.skip(f"Text index not found: {exc}")
            raise

        assert len(results) <= 2

    async def test_no_match_returns_empty(
        self, mongo: MongoClient, corpus: dict[str, str]
    ) -> None:
        retriever = MongoBM25Retriever(mongo)
        request = RetrievalRequest(
            query="zzzyyyxxx_nonexistent_token_12345",
            tenant_id=corpus["tenant_id"],
            category_id=corpus["category_id"],
        )
        try:
            results = await retriever.retrieve(request)
        except Exception as exc:
            if "text index" in str(exc).lower():
                pytest.skip(f"Text index not found: {exc}")
            raise

        assert results == []
