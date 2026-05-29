"""SourceRepository protocol."""

from __future__ import annotations

from typing import Protocol

from trendstorm.domain.sources.models import Source


class SourceRepository(Protocol):
    """Persistence contract for Source entities."""

    async def insert(self, source: Source) -> None:
        """Insert. Raises ConflictError on (tenant, url_hash) duplicate."""
        ...

    async def get(self, tenant_id: str, source_id: str) -> Source | None: ...

    async def list_by_category(
        self,
        tenant_id: str,
        category_id: str,
        *,
        enabled_only: bool = True,
        limit: int = 200,
    ) -> list[Source]:
        """List sources in a category.

        The 200 default limit covers almost all real categories; we can add
        cursoring if needed later.
        """
        ...

    async def list_by_ids(
        self,
        tenant_id: str,
        source_ids: list[str],
    ) -> list[Source]:
        """Bulk lookup. Used by the orchestrator when starting a job."""
        ...

    async def update_fetch_status(
        self,
        tenant_id: str,
        source_id: str,
        *,
        status: str,
        error: str | None = None,
        fetched_at: object,  # datetime; typed loosely to avoid circular import
    ) -> None:
        """Record the most recent fetch outcome. Called by Scout."""
        ...
