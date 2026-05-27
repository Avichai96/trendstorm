"""MongoDB implementation of TenantSettingsRepository."""
from __future__ import annotations

from typing import ClassVar

from trendstorm.domain.tenant_settings.models import TenantSettings
from trendstorm.infrastructure.mongo.repositories._base import (
    TenantScopedRepository,
    now_utc,
)
from trendstorm.infrastructure.mongo.schema import Collection


class MongoTenantSettingsRepository(TenantScopedRepository[TenantSettings]):
    collection: ClassVar[Collection] = Collection.TENANT_SETTINGS
    model: ClassVar[type[TenantSettings]] = TenantSettings

    async def get_for_tenant(self, tenant_id: str) -> TenantSettings | None:
        doc = await self._find_one(
            self._tenant_query(tenant_id),
            what=f"TenantSettings for tenant {tenant_id}",
        )
        return self._decode(doc) if doc else None

    async def upsert(self, settings: TenantSettings) -> TenantSettings:
        """Create-or-replace tenant settings. Last-write-wins."""
        data = self._encode(settings)
        data["updated_at"] = now_utc()
        doc = await self._coll.find_one_and_update(
            {"tenant_id": settings.tenant_id},
            {"$set": data},
            upsert=True,
            return_document=True,
        )
        return self._decode(doc)
