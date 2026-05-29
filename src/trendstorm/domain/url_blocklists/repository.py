"""UrlBlocklistRepository Protocol."""

from __future__ import annotations

from typing import Protocol

from trendstorm.domain.url_blocklists.models import UrlBlocklistEntry


class UrlBlocklistRepository(Protocol):
    async def list_for_tenant(self, tenant_id: str) -> list[UrlBlocklistEntry]:
        """Return all blocklist entries for a tenant."""
        ...

    async def insert(self, entry: UrlBlocklistEntry) -> None:
        """Add a new blocklist entry."""
        ...

    async def delete(self, tenant_id: str, entry_id: str) -> bool:
        """Remove an entry. Returns True if found and deleted."""
        ...
