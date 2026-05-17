"""Integration tests for the Phase 5 repositories.

Exercises the real Mongo against the seeded indexes. Requires `make up`.

Tests verify:
    - CRUD round-trips through Mongo.
    - Tenant isolation (one tenant's writes are invisible to another).
    - Unique constraints (duplicate category names rejected).
    - Bulk operations (chunks bulk_insert).
"""
from __future__ import annotations

import pytest

from trendstorm.domain.categories.models import Category
from trendstorm.domain.chunks.models import Chunk
from trendstorm.domain.documents.models import RawDocument
from trendstorm.domain.sources.models import Source
from trendstorm.infrastructure.mongo.client import MongoClient
from trendstorm.infrastructure.mongo.repositories import (
    MongoCategoryRepository,
    MongoChunkRepository,
    MongoRawDocumentRepository,
    MongoSourceRepository,
)
from trendstorm.shared.config import get_settings
from trendstorm.shared.errors import ConflictError
from trendstorm.shared.ids import new_id

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()


@pytest.fixture
async def mongo():
    """Connected Mongo client. Closes after the test."""
    settings = get_settings()
    client = MongoClient(settings.mongo)
    await client.connect()
    yield client
    await client.close()


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

class TestCategoryRepository:
    async def test_insert_and_get(self, mongo: MongoClient) -> None:
        repo = MongoCategoryRepository(mongo)
        cat = Category(tenant_id=new_id(), name=f"test-{new_id()[:8]}")
        await repo.insert(cat)
        fetched = await repo.get(cat.tenant_id, cat.id)
        assert fetched is not None
        assert fetched.id == cat.id
        assert fetched.name == cat.name

    async def test_get_by_name(self, mongo: MongoClient) -> None:
        repo = MongoCategoryRepository(mongo)
        tenant = new_id()
        cat = Category(tenant_id=tenant, name=f"unique-{new_id()[:8]}")
        await repo.insert(cat)
        fetched = await repo.get_by_name(tenant, cat.name)
        assert fetched is not None
        assert fetched.id == cat.id

    async def test_unique_name_per_tenant(self, mongo: MongoClient) -> None:
        """The unique index must reject a same-tenant duplicate name."""
        repo = MongoCategoryRepository(mongo)
        tenant = new_id()
        name = f"dup-{new_id()[:8]}"
        await repo.insert(Category(tenant_id=tenant, name=name))
        with pytest.raises(ConflictError):
            await repo.insert(Category(tenant_id=tenant, name=name))

    async def test_same_name_different_tenant_allowed(self, mongo: MongoClient) -> None:
        """Tenants A and B should both be able to have 'Crypto'."""
        repo = MongoCategoryRepository(mongo)
        name = f"shared-{new_id()[:8]}"
        await repo.insert(Category(tenant_id=new_id(), name=name))
        await repo.insert(Category(tenant_id=new_id(), name=name))   # different tenant — fine

    async def test_tenant_isolation_on_get(self, mongo: MongoClient) -> None:
        """Get with the wrong tenant returns None, never the doc."""
        repo = MongoCategoryRepository(mongo)
        a, b = new_id(), new_id()
        cat = Category(tenant_id=a, name=f"iso-{new_id()[:8]}")
        await repo.insert(cat)
        assert await repo.get(a, cat.id) is not None
        assert await repo.get(b, cat.id) is None

    async def test_update_partial(self, mongo: MongoClient) -> None:
        repo = MongoCategoryRepository(mongo)
        tenant = new_id()
        cat = Category(tenant_id=tenant, name=f"upd-{new_id()[:8]}", description="old")
        await repo.insert(cat)
        updated = await repo.update(tenant, cat.id, description="new")
        assert updated is not None
        assert updated.description == "new"
        # Other fields preserved.
        assert updated.name == cat.name


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

class TestSourceRepository:
    async def test_insert_and_get(self, mongo: MongoClient) -> None:
        repo = MongoSourceRepository(mongo)
        src = Source(
            tenant_id=new_id(),
            category_id=new_id(),
            url=f"https://example-{new_id()[:8]}.com/x",
        )
        await repo.insert(src)
        fetched = await repo.get(src.tenant_id, src.id)
        assert fetched is not None
        assert fetched.url == src.url
        assert fetched.url_hash == src.url_hash

    async def test_duplicate_url_rejected(self, mongo: MongoClient) -> None:
        """Same URL in same tenant must be rejected (even across categories).

        Decision: a URL is unique PER TENANT, not per category. Adding the
        same Wikipedia article under two categories doesn't fetch it twice.
        """
        repo = MongoSourceRepository(mongo)
        tenant = new_id()
        url = f"https://dup-{new_id()[:8]}.com/post"
        await repo.insert(Source(tenant_id=tenant, category_id=new_id(), url=url))
        with pytest.raises(ConflictError):
            await repo.insert(Source(tenant_id=tenant, category_id=new_id(), url=url))

    async def test_list_by_ids_preserves_order(self, mongo: MongoClient) -> None:
        repo = MongoSourceRepository(mongo)
        tenant = new_id()
        cat = new_id()
        sources = [
            Source(tenant_id=tenant, category_id=cat,
                   url=f"https://order-{i}-{new_id()[:8]}.com")
            for i in range(5)
        ]
        for s in sources:
            await repo.insert(s)

        # Request in reverse order.
        reversed_ids = [s.id for s in reversed(sources)]
        got = await repo.list_by_ids(tenant, reversed_ids)
        assert [s.id for s in got] == reversed_ids


# ---------------------------------------------------------------------------
# Raw documents
# ---------------------------------------------------------------------------

class TestRawDocumentRepository:
    async def test_find_by_content_hash(self, mongo: MongoClient) -> None:
        repo = MongoRawDocumentRepository(mongo)
        tenant = new_id()
        h = "deadbeef" * 8     # 64 hex chars
        doc = RawDocument(
            tenant_id=tenant,
            job_id=new_id(),
            category_id=new_id(),
            source_id=new_id(),
            url="https://example.com/x",
            content_hash=h,
        )
        await repo.insert(doc)

        found = await repo.find_by_content_hash(tenant, h)
        assert found is not None
        assert found.id == doc.id


# ---------------------------------------------------------------------------
# Chunks — exercises bulk_insert & get_many ordering
# ---------------------------------------------------------------------------

class TestChunkRepository:
    async def test_bulk_insert_count(self, mongo: MongoClient) -> None:
        repo = MongoChunkRepository(mongo)
        tenant = new_id()
        chunks = [
            Chunk(
                tenant_id=tenant,
                job_id=new_id(),
                category_id=new_id(),
                document_id=new_id(),
                source_id=new_id(),
                position=i,
                text=f"chunk text {i}",
            )
            for i in range(20)
        ]
        inserted = await repo.bulk_insert(chunks)
        assert inserted == 20

    async def test_get_many_preserves_order(self, mongo: MongoClient) -> None:
        """Vector-search returns IDs in score order; we MUST preserve it."""
        repo = MongoChunkRepository(mongo)
        tenant = new_id()
        chunks = [
            Chunk(
                tenant_id=tenant,
                job_id=new_id(),
                category_id=new_id(),
                document_id=new_id(),
                source_id=new_id(),
                position=i,
                text=f"order test {i}",
            )
            for i in range(10)
        ]
        await repo.bulk_insert(chunks)

        # Simulate vector-search returning a specific order.
        requested = [chunks[3].id, chunks[7].id, chunks[0].id, chunks[5].id]
        got = await repo.get_many(tenant, requested)
        assert [c.id for c in got] == requested

    async def test_empty_get_many_returns_empty(self, mongo: MongoClient) -> None:
        repo = MongoChunkRepository(mongo)
        result = await repo.get_many(new_id(), [])
        assert result == []
