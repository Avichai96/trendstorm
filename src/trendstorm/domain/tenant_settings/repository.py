"""TenantSettingsRepository Protocol."""
from __future__ import annotations

from typing import Protocol

from trendstorm.domain.tenant_settings.models import TenantSettings


class TenantSettingsRepository(Protocol):
    async def get_for_tenant(self, tenant_id: str) -> TenantSettings | None: ...

    async def upsert(self, settings: TenantSettings) -> TenantSettings: ...
