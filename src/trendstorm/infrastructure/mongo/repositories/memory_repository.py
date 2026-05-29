"""MongoDB implementation of MemoryRepository (Phase 15.5)."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from pymongo import DESCENDING

from trendstorm.domain.memories.models import Memory, MemoryKind
from trendstorm.infrastructure.mongo.repositories._base import (
    TenantScopedRepository,
    now_utc,
)
from trendstorm.infrastructure.mongo.schema import Collection
from trendstorm.shared.logging import get_logger

logger = get_logger(__name__)


class MongoMemoryRepository(TenantScopedRepository[Memory]):
    collection: ClassVar[Collection] = Collection.MEMORIES
    model: ClassVar[type[Memory]] = Memory

    async def insert(self, memory: Memory) -> None:
        await self._insert(self._encode(memory), what=f"Memory {memory.id}")

    async def get(self, tenant_id: str, memory_id: str) -> Memory | None:
        doc = await self._find_one(
            self._tenant_query(tenant_id, _id=memory_id),
            what=f"Memory {memory_id}",
        )
        return self._decode(doc) if doc else None

    async def list_active_for_category(
        self,
        tenant_id: str,
        category_id: str,
        *,
        kind: MemoryKind | None = None,
        limit: int = 50,
    ) -> list[Memory]:
        query = self._tenant_query(tenant_id, category_id=category_id, is_active=True)
        if kind is not None:
            query["kind"] = kind.value
        docs = await self._find_many(
            query,
            sort=[("_id", DESCENDING)],
            limit=limit,
            what="active Memory list",
        )
        return [self._decode(d) for d in docs]

    async def list_for_category(
        self,
        tenant_id: str,
        category_id: str,
        *,
        kind: MemoryKind | None = None,
        limit: int = 50,
        before_id: str | None = None,
    ) -> list[Memory]:
        query = self._tenant_query(tenant_id, category_id=category_id)
        if kind is not None:
            query["kind"] = kind.value
        if before_id is not None:
            query["_id"] = {"$lt": before_id}
        docs = await self._find_many(
            query,
            sort=[("_id", DESCENDING)],
            limit=limit,
            what="Memory list",
        )
        return [self._decode(d) for d in docs]

    async def set_embedding_id(
        self,
        tenant_id: str,
        memory_id: str,
        embedding_id: str,
    ) -> None:
        now = now_utc()
        await self._coll.update_one(
            self._tenant_query(tenant_id, _id=memory_id),
            {"$set": {"content_embedding_id": embedding_id, "updated_at": now}},
        )

    async def supersede(
        self,
        tenant_id: str,
        old_memory_id: str,
        superseded_by_id: str,
    ) -> None:
        now = now_utc()
        await self._coll.update_one(
            self._tenant_query(tenant_id, _id=old_memory_id, is_active=True),
            {
                "$set": {
                    "superseded_by": superseded_by_id,
                    "is_active": False,
                    "updated_at": now,
                }
            },
        )
        logger.info(
            "memory.superseded",
            tenant_id=tenant_id,
            old_id=old_memory_id,
            new_id=superseded_by_id,
        )

    async def deactivate(self, tenant_id: str, memory_id: str) -> None:
        now = now_utc()
        await self._coll.update_one(
            self._tenant_query(tenant_id, _id=memory_id),
            {"$set": {"is_active": False, "updated_at": now}},
        )

    async def exists_for_job(self, tenant_id: str, job_id: str) -> bool:
        """Check if any memory already exists for this job (idempotency guard)."""
        doc = await self._find_one(
            self._tenant_query(tenant_id, source_job_id=job_id),
            what=f"Memory for job {job_id}",
        )
        return doc is not None
