"""Auth repository protocols."""
from __future__ import annotations

from typing import Protocol

from trendstorm.domain.auth.models import ApiKey, Tenant


class TenantRepository(Protocol):
    """Persistence contract for Tenants."""

    async def insert(self, tenant: Tenant) -> None: ...

    async def get(self, tenant_id: str) -> Tenant | None: ...

    async def get_by_name(self, name: str) -> Tenant | None: ...


class ApiKeyRepository(Protocol):
    """Persistence contract for ApiKey credentials."""

    async def insert(self, key: ApiKey) -> None: ...

    async def get_by_hash(self, key_hash: str) -> ApiKey | None:
        """Primary lookup path on each request. Must be O(1) via index on key_hash."""
        ...

    async def get_by_id(self, tenant_id: str, key_id: str) -> ApiKey | None: ...

    async def list_for_tenant(self, tenant_id: str) -> list[ApiKey]:
        """Return all keys (revoked and active) for the tenant."""
        ...

    async def revoke(self, tenant_id: str, key_id: str) -> None:
        """Stamp `revoked_at = now()`. Idempotent if already revoked."""
        ...

    async def update_last_used(self, key_id: str) -> None:
        """Stamp `last_used_at = now()`. Best-effort; caller may fire-and-forget."""
        ...
