"""ChromaDB vector store for long-term memories (Phase 15.5).

Collection naming convention:
    memories__{tenant_id[:8].lower()}__{model_id_safe}
    e.g. "memories__01hxabcd__gemini_text_embedding_004"

Kept separate from the chunk store (chroma_store.py) because:
    - Memory TTL (2 years) vs chunk TTL (1 year) — different lifecycle.
    - Memory queries include kind filter; chunk queries do not.
    - Separate collections prevent score pollution between short-lived
      chunk evidence and long-lived memory claims during RRF merging.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from trendstorm.domain.vectors.models import VectorHit
from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    from trendstorm.shared.config import VectorSettings

logger = get_logger(__name__)

_COSINE_DISTANCE_MAX = 2.0


def _to_score(distance: float) -> float:
    """Convert ChromaDB cosine distance [0, 2] to similarity [0, 1]."""
    return max(0.0, min(1.0, 1.0 - distance / _COSINE_DISTANCE_MAX))


def memory_collection_name(tenant_id: str, model_id: str) -> str:
    """Canonical ChromaDB collection name for memories."""
    model_safe = model_id.replace(".", "_").replace("-", "_")
    return f"memories__{tenant_id[:8].lower()}__{model_safe}"


class ChromaMemoryStore:
    """Async ChromaDB store for memory embeddings.

    Wraps the ChromaDB HTTP client directly (same pattern as ChromaVectorStore)
    so lifecycle management stays uniform across callers.

    Pass _client to inject a test double; omit for production.
    """

    def __init__(
        self,
        settings: VectorSettings,
        *,
        _client: Any = None,
    ) -> None:
        self._settings = settings
        self._client: Any = _client
        self._coll_cache: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Create the ChromaDB async HTTP client. Idempotent."""
        if self._client is not None:
            return
        import chromadb  # deferred

        self._client = await chromadb.AsyncHttpClient(
            host=self._settings.chroma_host,
            port=self._settings.chroma_port,
        )
        self._coll_cache.clear()
        logger.info(
            "chroma_memory.connected",
            host=self._settings.chroma_host,
            port=self._settings.chroma_port,
        )

    async def close(self) -> None:
        self._client = None
        self._coll_cache.clear()

    async def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            await self._client.heartbeat()
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Memory-specific operations
    # ------------------------------------------------------------------

    async def upsert_memory(
        self,
        *,
        collection: str,
        memory_id: str,
        embedding: list[float],
        content: str,
        metadata: dict[str, Any],
    ) -> None:
        """Insert or overwrite one memory vector."""
        coll = await self._get_collection(collection)
        await coll.upsert(
            ids=[memory_id],
            embeddings=[embedding],
            documents=[content],
            metadatas=[metadata],
        )
        logger.debug("chroma_memory.upsert", collection=collection, memory_id=memory_id)

    async def query_memories(
        self,
        *,
        collection: str,
        query_embedding: list[float],
        n_results: int,
        tenant_id: str,
        category_id: str,
        kind: str | None = None,
    ) -> list[VectorHit]:
        """Return top-n nearest memory neighbours.

        Always filters to (tenant_id, category_id); kind is optional.
        The $and filter ensures cross-category isolation.
        """
        coll = await self._get_collection(collection)
        where_clauses: list[dict[str, Any]] = [
            {"tenant_id": {"$eq": tenant_id}},
            {"category_id": {"$eq": category_id}},
            {"is_active": {"$eq": True}},
        ]
        if kind is not None:
            where_clauses.append({"kind": {"$eq": kind}})
        where: dict[str, Any] = (
            {"$and": where_clauses} if len(where_clauses) > 1 else where_clauses[0]
        )

        try:
            results = await coll.query(
                query_embeddings=[query_embedding],
                n_results=n_results,
                where=where,
            )
        except Exception as exc:
            logger.warning("chroma_memory.query_error", error=str(exc), collection=collection)
            return []

        ids: list[str] = results["ids"][0]
        distances: list[float] = results["distances"][0]
        metas: list[dict[str, Any]] = (results.get("metadatas") or [[]])[0] or [{}] * len(ids)
        docs: list[str | None] = (results.get("documents") or [[None] * len(ids)])[0]

        return [
            VectorHit(
                id=mid,
                score=_to_score(dist),
                metadata=meta or {},
                document=doc,
            )
            for mid, dist, meta, doc in zip(ids, distances, metas, docs, strict=True)
        ]

    async def delete_memory(self, *, collection: str, memory_id: str) -> None:
        """Remove a memory vector from ChromaDB."""
        coll = await self._get_collection(collection)
        await coll.delete(ids=[memory_id])
        logger.debug("chroma_memory.delete", collection=collection, memory_id=memory_id)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _get_collection(self, name: str) -> Any:
        if name not in self._coll_cache:
            self._coll_cache[name] = await self._client.get_or_create_collection(
                name=name,
                metadata={"hnsw:space": "cosine"},
            )
        return self._coll_cache[name]
