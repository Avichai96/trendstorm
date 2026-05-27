"""MongoDB implementation of UrlBlocklistRepository."""
from __future__ import annotations

from typing import ClassVar

from pymongo import DESCENDING

from trendstorm.domain.url_blocklists.models import UrlBlocklistEntry
from trendstorm.infrastructure.mongo.repositories._base import TenantScopedRepository
from trendstorm.infrastructure.mongo.schema import Collection


class MongoUrlBlocklistRepository(TenantScopedRepository[UrlBlocklistEntry]):
    """Per-tenant URL blocklist rules backed by MongoDB."""

    collection: ClassVar[Collection] = Collection.URL_BLOCKLISTS
    model: ClassVar[type[UrlBlocklistEntry]] = UrlBlocklistEntry

    async def list_for_tenant(self, tenant_id: str) -> list[UrlBlocklistEntry]:
        docs = await self._find_many(
            self._tenant_query(tenant_id),
            sort=[("created_at", DESCENDING)],
            what="UrlBlocklistEntry list",
        )
        return [self._decode(d) for d in docs]

    async def insert(self, entry: UrlBlocklistEntry) -> None:
        await self._insert(self._encode(entry), what=f"UrlBlocklistEntry {entry.id}")

    async def delete(self, tenant_id: str, entry_id: str) -> bool:
        try:
            result = await self._coll.delete_one(
                self._tenant_query(tenant_id, _id=entry_id)
            )
            return result.deleted_count > 0
        except Exception:
            return False
