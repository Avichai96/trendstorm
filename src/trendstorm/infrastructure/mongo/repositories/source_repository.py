"""MongoDB implementation of SourceRepository."""
from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from pymongo.errors import PyMongoError

from trendstorm.domain.sources.models import Source
from trendstorm.infrastructure.mongo.repositories._base import (
    TenantScopedRepository,
    now_utc,
    raise_db_error,
)
from trendstorm.infrastructure.mongo.schema import Collection
from trendstorm.shared.logging import get_logger

logger = get_logger(__name__)


class MongoSourceRepository(TenantScopedRepository[Source]):
    """Concrete SourceRepository backed by MongoDB."""

    collection: ClassVar[Collection] = Collection.SOURCES
    model: ClassVar[type[Source]] = Source

    async def insert(self, source: Source) -> None:
        await self._insert(self._encode(source), what=f"Source {source.url}")

    async def get(self, tenant_id: str, source_id: str) -> Source | None:
        doc = await self._find_one(
            self._tenant_query(tenant_id, _id=source_id),
            what=f"Source {source_id}",
        )
        return self._decode(doc) if doc else None

    async def list_by_category(
        self,
        tenant_id: str,
        category_id: str,
        *,
        enabled_only: bool = True,
        limit: int = 200,
    ) -> list[Source]:
        query = self._tenant_query(tenant_id, category_id=category_id)
        if enabled_only:
            query["enabled"] = True
        docs = await self._find_many(
            query,
            sort=[("_id", -1)],
            limit=limit,
            what="sources list",
        )
        return [self._decode(d) for d in docs]

    async def list_by_ids(
        self,
        tenant_id: str,
        source_ids: list[str],
    ) -> list[Source]:
        """Bulk lookup by id. Preserves the caller-requested order.

        Why preserve order? The caller (JobService) often wants to fan out
        ingestion in a deterministic order so progress bars and partial
        results align with the user's source list ordering.
        """
        if not source_ids:
            return []
        query = self._tenant_query(tenant_id, _id={"$in": source_ids})
        docs = await self._find_many(query, what="sources bulk")
        decoded = [self._decode(d) for d in docs]
        # Build position map for stable ordering.
        order = {sid: i for i, sid in enumerate(source_ids)}
        decoded.sort(key=lambda s: order.get(s.id, len(source_ids)))
        return decoded

    async def update_fetch_status(
        self,
        tenant_id: str,
        source_id: str,
        *,
        status: str,
        error: str | None = None,
        fetched_at: object,    # datetime
    ) -> None:
        # We accept `object` in the protocol to avoid circular imports;
        # narrow to datetime here for type safety.
        if not isinstance(fetched_at, datetime):
            raise TypeError("fetched_at must be a datetime")

        update: dict[str, object] = {
            "$set": {
                "last_fetch_at": fetched_at,
                "last_fetch_status": status,
                "last_fetch_error": error,
                "updated_at": now_utc(),
            }
        }
        try:
            await self._coll.update_one(
                self._tenant_query(tenant_id, _id=source_id),
                update,
            )
        except PyMongoError as e:
            raise_db_error(e, operation="update_fetch_status", source_id=source_id)
