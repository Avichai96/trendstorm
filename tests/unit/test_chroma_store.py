"""Unit tests for ChromaVectorStore using an injected fake ChromaDB client.

No real ChromaDB connection; the fake mirrors the AsyncHttpClient interface.
"""
from __future__ import annotations

from typing import Any

import pytest

from trendstorm.domain.vectors.models import VectorHit
from trendstorm.domain.vectors.store import VectorStore
from trendstorm.infrastructure.vectors.chroma_store import ChromaVectorStore, _to_score
from trendstorm.shared.config import Settings

# ---------------------------------------------------------------------------
# Fake ChromaDB client
# ---------------------------------------------------------------------------


class _FakeCollection:
    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}

    async def upsert(
        self,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: list[dict[str, Any]],
    ) -> None:
        for i, id_ in enumerate(ids):
            self._data[id_] = {
                "embedding": embeddings[i],
                "document": documents[i],
                "metadata": metadatas[i],
            }

    async def query(
        self,
        query_embeddings: list[list[float]],
        n_results: int,
        where: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        items = list(self._data.items())
        if where:
            # Simplified filter: only supports {"key": {"$eq": value}}
            items = [
                (k, v)
                for k, v in items
                if all(
                    v["metadata"].get(fk) == fv.get("$eq", fv)
                    for fk, fv in where.items()
                )
            ]
        items = items[:n_results]
        ids = [item[0] for item in items]
        distances = [0.1 * i for i in range(len(ids))]
        metadatas = [item[1]["metadata"] for item in items]
        documents = [item[1]["document"] for item in items]
        return {
            "ids": [ids],
            "distances": [distances],
            "metadatas": [metadatas],
            "documents": [documents],
        }

    async def delete(self, where: dict[str, Any] | None = None) -> None:
        if where is None:
            self._data.clear()
            return
        to_delete = [
            k
            for k, v in self._data.items()
            if all(
                v["metadata"].get(fk) == fv.get("$eq", fv) for fk, fv in where.items()
            )
        ]
        for k in to_delete:
            del self._data[k]


class _FakeChromaClient:
    def __init__(self, healthy: bool = True) -> None:
        self._collections: dict[str, _FakeCollection] = {}
        self._healthy = healthy

    async def get_or_create_collection(
        self, name: str, metadata: dict[str, Any] | None = None
    ) -> _FakeCollection:
        if name not in self._collections:
            self._collections[name] = _FakeCollection()
        return self._collections[name]

    async def heartbeat(self) -> int:
        if not self._healthy:
            raise OSError("connection refused")
        return 1_000_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(healthy: bool = True) -> ChromaVectorStore:
    settings = Settings().vector
    return ChromaVectorStore(settings, _client=_FakeChromaClient(healthy))


def _vec(n: int = 4, val: float = 0.1) -> list[float]:
    return [val] * n


# ---------------------------------------------------------------------------
# Tests: score conversion
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestToScore:
    def test_distance_zero_is_one(self) -> None:
        assert _to_score(0.0) == pytest.approx(1.0)

    def test_distance_two_is_zero(self) -> None:
        assert _to_score(2.0) == pytest.approx(0.0)

    def test_distance_one_is_half(self) -> None:
        assert _to_score(1.0) == pytest.approx(0.5)

    def test_negative_distance_clamped_to_one(self) -> None:
        assert _to_score(-0.5) == pytest.approx(1.0)

    def test_distance_above_two_clamped_to_zero(self) -> None:
        assert _to_score(2.5) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Tests: lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestChromaStoreLifecycle:
    async def test_health_check_true_when_connected(self) -> None:
        store = _make_store(healthy=True)
        assert await store.health_check() is True

    async def test_health_check_false_when_client_down(self) -> None:
        store = _make_store(healthy=False)
        assert await store.health_check() is False

    async def test_health_check_false_before_connect(self) -> None:
        settings = Settings().vector
        store = ChromaVectorStore(settings)  # no _client, no connect()
        assert await store.health_check() is False

    async def test_close_clears_collection_cache(self) -> None:
        store = _make_store()
        await store.upsert("col", ["id1"], [_vec()], ["doc"], [{"k": "v"}])
        assert "col" in store._coll_cache
        await store.close()
        assert store._coll_cache == {}

    def test_satisfies_vector_store_protocol(self) -> None:
        assert isinstance(_make_store(), VectorStore)


# ---------------------------------------------------------------------------
# Tests: upsert + query
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestChromaStoreOperations:
    async def test_upsert_and_query_basic(self) -> None:
        store = _make_store()
        await store.upsert(
            "col",
            ids=["c1", "c2"],
            embeddings=[_vec(), _vec(val=0.2)],
            documents=["text one", "text two"],
            metadatas=[{"cat": "a"}, {"cat": "b"}],
        )
        hits = await store.query("col", query_embedding=_vec(), n_results=2)
        assert len(hits) == 2
        assert all(isinstance(h, VectorHit) for h in hits)

    async def test_query_score_in_range(self) -> None:
        store = _make_store()
        await store.upsert("col", ["id1"], [_vec()], ["text"], [{}])
        hits = await store.query("col", _vec(), n_results=1)
        assert 0.0 <= hits[0].score <= 1.0

    async def test_query_empty_collection_returns_empty(self) -> None:
        store = _make_store()
        # Don't upsert anything — empty collection returns no results
        hits = await store.query("empty_col", _vec(), n_results=5)
        assert hits == []

    async def test_query_with_metadata_filter(self) -> None:
        store = _make_store()
        await store.upsert(
            "col",
            ["c1", "c2"],
            [_vec(), _vec()],
            ["doc1", "doc2"],
            [{"cat": "x"}, {"cat": "y"}],
        )
        hits = await store.query(
            "col", _vec(), n_results=5, where={"cat": {"$eq": "x"}}
        )
        assert len(hits) == 1
        assert hits[0].id == "c1"

    async def test_query_hits_include_document(self) -> None:
        store = _make_store()
        await store.upsert("col", ["id1"], [_vec()], ["the text"], [{}])
        hits = await store.query("col", _vec(), n_results=1)
        assert hits[0].document == "the text"

    async def test_query_hits_include_metadata(self) -> None:
        store = _make_store()
        await store.upsert("col", ["id1"], [_vec()], ["doc"], [{"source_id": "s1"}])
        hits = await store.query("col", _vec(), n_results=1)
        assert hits[0].metadata == {"source_id": "s1"}

    async def test_different_collections_are_isolated(self) -> None:
        store = _make_store()
        await store.upsert("col_a", ["a1"], [_vec()], ["doc_a"], [{}])
        await store.upsert("col_b", ["b1"], [_vec()], ["doc_b"], [{}])
        hits_a = await store.query("col_a", _vec(), n_results=5)
        hits_b = await store.query("col_b", _vec(), n_results=5)
        assert [h.id for h in hits_a] == ["a1"]
        assert [h.id for h in hits_b] == ["b1"]

    async def test_delete_by_filter_removes_matching(self) -> None:
        store = _make_store()
        await store.upsert(
            "col",
            ["c1", "c2"],
            [_vec(), _vec()],
            ["doc1", "doc2"],
            [{"doc_id": "d1"}, {"doc_id": "d2"}],
        )
        await store.delete_by_filter("col", where={"doc_id": {"$eq": "d1"}})
        hits = await store.query("col", _vec(), n_results=5)
        ids = [h.id for h in hits]
        assert "c1" not in ids
        assert "c2" in ids

    async def test_upsert_overwrites_existing_id(self) -> None:
        store = _make_store()
        await store.upsert("col", ["id1"], [_vec()], ["original"], [{"v": 1}])
        await store.upsert("col", ["id1"], [_vec(val=0.5)], ["updated"], [{"v": 2}])
        hits = await store.query("col", _vec(), n_results=1)
        assert hits[0].document == "updated"
        assert hits[0].metadata == {"v": 2}

    async def test_collection_cache_reused(self) -> None:
        store = _make_store()
        await store.upsert("my_col", ["id1"], [_vec()], ["d"], [{}])
        assert "my_col" in store._coll_cache
        # Second call should use cache, not re-create
        coll_first = store._coll_cache["my_col"]
        await store.upsert("my_col", ["id2"], [_vec()], ["d2"], [{}])
        assert store._coll_cache["my_col"] is coll_first
