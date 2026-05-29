"""ChromaDB vector store implementation.

Collection naming convention:
    f"chunks__{tenant_id[:8]}__{model_id.replace('.', '_').replace('-', '_')}"
    e.g. "chunks__01HXABCD__gemini_text_embedding_004"

The collection name is constructed by the knowledge pipeline (agents/knowledge/)
and passed in; this class treats it as an opaque string.

Distance → score conversion (cosine space):
    ChromaDB returns cosine distance = 1 - cosine_similarity ∈ [0, 2].
    VectorHit.score ∈ [0, 1] where 1 = identical, 0 = opposite.
    Formula: score = max(0, min(1, 1 - distance / 2)).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from trendstorm.domain.vectors.models import VectorHit
from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    from trendstorm.shared.config import VectorSettings

logger = get_logger(__name__)


def _to_score(distance: float) -> float:
    """Convert ChromaDB cosine distance [0, 2] to similarity score [0, 1]."""
    return max(0.0, min(1.0, 1.0 - distance / 2.0))


class ChromaVectorStore:
    """Async ChromaDB vector store implementing the VectorStore Protocol.

    Lifecycle: call connect() before use, close() on shutdown.
    The ChromaDB HTTP client is created lazily in connect() — importing
    chromadb at module load is deferred to avoid hard dependency when the
    store is not used.

    Pass _client to inject a fake for unit tests; omit for production.
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
        import chromadb  # deferred — only needed for real connections

        self._client = await chromadb.AsyncHttpClient(
            host=self._settings.chroma_host,
            port=self._settings.chroma_port,
        )
        self._coll_cache.clear()
        logger.info(
            "chroma.connected",
            host=self._settings.chroma_host,
            port=self._settings.chroma_port,
        )

    async def close(self) -> None:
        """Release the client and clear the collection cache."""
        self._client = None
        self._coll_cache.clear()

    # ------------------------------------------------------------------
    # VectorStore Protocol
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        """Return True if ChromaDB is reachable."""
        if self._client is None:
            return False
        try:
            await self._client.heartbeat()
            return True
        except Exception:
            return False

    async def upsert(
        self,
        collection: str,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: list[dict[str, Any]],
    ) -> None:
        """Insert or overwrite vectors in the named collection."""
        coll = await self._get_collection(collection)
        await coll.upsert(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas,
        )
        logger.debug("chroma.upsert", collection=collection, n=len(ids))

    async def query(
        self,
        collection: str,
        query_embedding: list[float],
        n_results: int,
        where: dict[str, Any] | None = None,
    ) -> list[VectorHit]:
        """Return top-n_results nearest neighbours, score-sorted descending."""
        coll = await self._get_collection(collection)
        results = await coll.query(
            query_embeddings=[query_embedding],
            n_results=n_results,
            where=where,
            # include defaults: metadatas, documents, distances
        )
        ids: list[str] = results["ids"][0]
        distances: list[float] = results["distances"][0]
        metas: list[dict[str, Any]] = (results.get("metadatas") or [[]])[0] or [{}] * len(ids)
        docs: list[str | None] = (results.get("documents") or [[None] * len(ids)])[0]

        return [
            VectorHit(
                id=chunk_id,
                score=_to_score(dist),
                metadata=meta or {},
                document=doc,
            )
            for chunk_id, dist, meta, doc in zip(ids, distances, metas, docs, strict=True)
        ]

    async def delete_by_filter(
        self,
        collection: str,
        where: dict[str, Any],
    ) -> None:
        """Delete all vectors matching the metadata filter."""
        coll = await self._get_collection(collection)
        await coll.delete(where=where)
        logger.debug("chroma.delete_by_filter", collection=collection, where=where)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_collection(self, name: str) -> Any:
        """Return (and cache) a cosine-space collection, creating if absent."""
        if name not in self._coll_cache:
            self._coll_cache[name] = await self._client.get_or_create_collection(
                name=name,
                metadata={"hnsw:space": "cosine"},
            )
        return self._coll_cache[name]
