"""ChunkRepository protocol."""

from __future__ import annotations

from typing import Protocol

from trendstorm.domain.chunks.models import Chunk


class ChunkRepository(Protocol):
    """Persistence contract for Chunks."""

    async def bulk_insert(self, chunks: list[Chunk]) -> int:
        """Insert many at once. Returns count inserted.

        Chunking produces dozens-to-hundreds of chunks per document; we
        never insert them one at a time. The implementation uses Mongo's
        bulk-write API with ordered=False so a single duplicate doesn't
        stop the whole batch.
        """
        ...

    async def get(self, tenant_id: str, chunk_id: str) -> Chunk | None: ...

    async def get_many(
        self,
        tenant_id: str,
        chunk_ids: list[str],
    ) -> list[Chunk]:
        """Bulk lookup by id. THE hot-path method.

        Called immediately after a vector search returns top-K IDs. The
        list MUST preserve input order if we want reranker scores to align.
        Implementation enforces this with a position map post-fetch.
        """
        ...

    async def list_by_document(
        self,
        tenant_id: str,
        document_id: str,
        *,
        embedding_model: str | None = None,
    ) -> list[Chunk]:
        """All chunks of a doc, in position order.

        embedding_model: optional filter to only child chunks of a specific model.
        Omit to return all chunks (parents + children, all models).
        """
        ...

    async def set_vector_id(
        self,
        tenant_id: str,
        chunk_id: str,
        vector_id: str,
        embedding_model: str,
    ) -> None:
        """Call after a successful Chroma write; closes the chunk lifecycle."""
        ...
