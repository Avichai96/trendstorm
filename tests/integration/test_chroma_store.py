"""Integration test: ChromaVectorStore against the live Chroma compose service.

Requires `make up` (starts ChromaDB on localhost:8000).
Skipped automatically if ChromaDB is unreachable.

Run manually:
    uv run pytest tests/integration/test_chroma_store.py -m integration -s
"""
from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator

import pytest

from trendstorm.domain.vectors.models import VectorHit
from trendstorm.infrastructure.vectors.chroma_store import ChromaVectorStore
from trendstorm.shared.config import get_settings
from trendstorm.shared.ids import new_id

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()


@pytest.fixture
async def store() -> AsyncGenerator[ChromaVectorStore, None]:
    """Connected ChromaVectorStore. Skips if Chroma is not reachable."""
    settings = get_settings()
    s = ChromaVectorStore(settings.vector)
    try:
        await s.connect()
        if not await s.health_check():
            pytest.skip("ChromaDB not healthy")
    except Exception as e:
        pytest.skip(f"ChromaDB not reachable: {e}")
    yield s
    await s.close()


@pytest.fixture
async def col(store: ChromaVectorStore) -> AsyncGenerator[str, None]:
    """Unique collection name per test. Deletes the collection on teardown."""
    # Use the random suffix of the ULID (characters 10-25) to ensure uniqueness
    # even when multiple tests run within the same millisecond. The first 10
    # chars of a ULID encode time, so they can collide; the tail is random.
    name = f"test__{new_id()[10:18].lower()}"
    yield name
    with contextlib.suppress(Exception):
        await store._client.delete_collection(name)


def _vec(n: int = 4, val: float = 0.1) -> list[float]:
    return [val] * n


async def test_upsert_and_query(store: ChromaVectorStore, col: str) -> None:
    """Upsert two vectors, query for nearest neighbour, verify structure."""
    await store.upsert(
        col,
        ids=["chunk-1", "chunk-2"],
        embeddings=[_vec(val=0.9), _vec(val=0.1)],
        documents=["Document one content.", "Document two content."],
        metadatas=[
            {"tenant_id": "t1", "category_id": "cat-a"},
            {"tenant_id": "t1", "category_id": "cat-b"},
        ],
    )

    hits = await store.query(col, query_embedding=_vec(val=0.9), n_results=2)

    assert len(hits) == 2
    assert all(isinstance(h, VectorHit) for h in hits)
    assert all(0.0 <= h.score <= 1.0 for h in hits)
    # Most similar result should be chunk-1 (same direction as query)
    assert hits[0].id == "chunk-1"


async def test_query_with_metadata_filter(store: ChromaVectorStore, col: str) -> None:
    """Metadata filter isolates results by category_id."""
    await store.upsert(
        col,
        ids=["c1", "c2", "c3"],
        embeddings=[_vec(), _vec(), _vec()],
        documents=["d1", "d2", "d3"],
        metadatas=[
            {"category_id": "cat-a"},
            {"category_id": "cat-b"},
            {"category_id": "cat-a"},
        ],
    )

    hits = await store.query(
        col, _vec(), n_results=10, where={"category_id": {"$eq": "cat-a"}}
    )
    assert all(h.metadata.get("category_id") == "cat-a" for h in hits)
    assert len(hits) == 2
    assert {h.id for h in hits} == {"c1", "c3"}


async def test_delete_by_filter(store: ChromaVectorStore, col: str) -> None:
    """delete_by_filter removes matching vectors; non-matching remain."""
    await store.upsert(
        col,
        ids=["keep", "delete-me"],
        embeddings=[_vec(), _vec()],
        documents=["keep", "delete"],
        metadatas=[{"doc_id": "d1"}, {"doc_id": "d2"}],
    )

    await store.delete_by_filter(col, where={"doc_id": {"$eq": "d2"}})

    hits = await store.query(col, _vec(), n_results=10)
    assert [h.id for h in hits] == ["keep"]


async def test_health_check_live(store: ChromaVectorStore) -> None:
    """health_check returns True for a connected, healthy store."""
    assert await store.health_check() is True


async def test_upsert_overwrites_on_same_id(store: ChromaVectorStore, col: str) -> None:
    """Upserting with the same id overwrites the previous vector."""
    await store.upsert(col, ["id1"], [_vec(val=0.1)], ["original"], [{"v": 1}])
    await store.upsert(col, ["id1"], [_vec(val=0.9)], ["updated"], [{"v": 2}])

    hits = await store.query(col, _vec(), n_results=1)
    assert hits[0].id == "id1"
    assert hits[0].document == "updated"
    assert hits[0].metadata.get("v") == 2
