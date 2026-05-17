"""VectorStore Protocol — the only interface the domain knows about vector storage.

Collection naming convention (enforced by the concrete implementation):
    f"chunks__{tenant_short}__{model_id}"
where tenant_short is the first 8 chars of the ULID and model_id is the
canonical "{provider}.{model_name}" from EmbeddingProvider.model_id.
Different embedding models create different collections; they are NEVER mixed.

Metadata keys stored at upsert time and available for filtering at query time:
    tenant_id   — tenant isolation (all queries must filter by this)
    category_id — prevents cross-category result bleed
    document_id — which RawDocument this chunk came from
    source_id   — denormalized from the document for provenance display
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from trendstorm.domain.vectors.models import VectorHit


@runtime_checkable
class VectorStore(Protocol):
    """Async interface for dense vector storage and retrieval."""

    async def health_check(self) -> bool:
        """Return True if the store is reachable and accepting requests."""
        ...

    async def upsert(
        self,
        collection: str,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: list[dict[str, Any]],
    ) -> None:
        """Insert or overwrite vectors.

        ids, embeddings, documents, and metadatas must all have the same length.
        Upsert semantics: existing ids are overwritten; new ids are inserted.
        """
        ...

    async def query(
        self,
        collection: str,
        query_embedding: list[float],
        n_results: int,
        where: dict[str, Any] | None = None,
    ) -> list[VectorHit]:
        """Return the top-n_results nearest neighbours.

        where is an optional metadata filter in the store's native format.
        Results are ordered by score descending (most similar first).
        """
        ...

    async def delete_by_filter(
        self,
        collection: str,
        where: dict[str, Any],
    ) -> None:
        """Delete all vectors matching the given metadata filter.

        Used to clean up chunks when a document is re-ingested or deleted.
        """
        ...
