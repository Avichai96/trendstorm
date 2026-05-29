"""MongoDB implementation of ChunkRepository.

Two methods deserve extra attention because they're on the hot path:

`bulk_insert`:
    Called once per document, with ~10-50 chunks. We use `insert_many` with
    `ordered=False` so a single duplicate (rare — chunk IDs are ULIDs) doesn't
    abort the whole batch. `ordered=True` is the default, and it stops on
    the first error — exactly what we don't want.

`get_many`:
    Called after every vector search. The caller passes IDs in score order
    (Chroma's ranking). We MUST return them in the same order so reranker
    scores align with the right text. Mongo's `$in` returns documents in
    no particular order, so we build a position map post-fetch.
"""

from __future__ import annotations

from typing import ClassVar

from pymongo import InsertOne
from pymongo.errors import BulkWriteError, PyMongoError

from trendstorm.domain.chunks.models import Chunk
from trendstorm.infrastructure.mongo.repositories._base import (
    TenantScopedRepository,
    now_utc,
    raise_db_error,
)
from trendstorm.infrastructure.mongo.schema import Collection
from trendstorm.shared.logging import get_logger

logger = get_logger(__name__)


class MongoChunkRepository(TenantScopedRepository[Chunk]):
    """Concrete ChunkRepository backed by MongoDB."""

    collection: ClassVar[Collection] = Collection.CHUNKS
    model: ClassVar[type[Chunk]] = Chunk

    async def bulk_insert(self, chunks: list[Chunk]) -> int:
        if not chunks:
            return 0

        ops = [InsertOne(self._encode(c)) for c in chunks]
        try:
            # ordered=False: keep going on individual errors; we'd rather
            # have 49 of 50 chunks than 0.
            result = await self._coll.bulk_write(ops, ordered=False)
            return result.inserted_count
        except BulkWriteError as e:
            # Partial success — extract what made it in. The `details`
            # dict has "nInserted" with the count.
            inserted = e.details.get("nInserted", 0) if e.details else 0
            logger.warning(
                "chunks_bulk_insert_partial",
                requested=len(chunks),
                inserted=inserted,
                errors=len(e.details.get("writeErrors", [])) if e.details else 0,
            )
            return int(inserted)
        except PyMongoError as e:
            raise_db_error(e, operation="bulk_insert chunks", count=len(chunks))
            return 0  # unreachable

    async def get(self, tenant_id: str, chunk_id: str) -> Chunk | None:
        doc = await self._find_one(
            self._tenant_query(tenant_id, _id=chunk_id),
            what=f"Chunk {chunk_id}",
        )
        return self._decode(doc) if doc else None

    async def get_many(
        self,
        tenant_id: str,
        chunk_ids: list[str],
    ) -> list[Chunk]:
        """Bulk lookup. Returns chunks in caller-supplied order."""
        if not chunk_ids:
            return []

        docs = await self._find_many(
            self._tenant_query(tenant_id, _id={"$in": chunk_ids}),
            what="chunks bulk",
        )
        # Build position map to enforce input ordering.
        order = {cid: i for i, cid in enumerate(chunk_ids)}
        decoded = [self._decode(d) for d in docs]
        decoded.sort(key=lambda c: order.get(c.id, len(chunk_ids)))
        return decoded

    async def list_by_document(
        self,
        tenant_id: str,
        document_id: str,
        *,
        embedding_model: str | None = None,
    ) -> list[Chunk]:
        query = self._tenant_query(tenant_id, document_id=document_id)
        if embedding_model is not None:
            query["embedding_model"] = embedding_model
        docs = await self._find_many(
            query,
            sort=[("position", 1)],
            what="chunks by document",
        )
        return [self._decode(d) for d in docs]

    async def set_vector_id(
        self,
        tenant_id: str,
        chunk_id: str,
        vector_id: str,
        embedding_model: str,
    ) -> None:
        try:
            await self._coll.update_one(
                self._tenant_query(tenant_id, _id=chunk_id),
                {
                    "$set": {
                        "vector_id": vector_id,
                        "embedding_model": embedding_model,
                        "updated_at": now_utc(),
                    }
                },
            )
        except PyMongoError as e:
            raise_db_error(e, operation="set_vector_id", chunk_id=chunk_id)
