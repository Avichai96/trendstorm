"""CategoryRepository protocol."""
from __future__ import annotations

from typing import Protocol

from trendstorm.domain.categories.models import Category


class CategoryRepository(Protocol):
    """Persistence contract for Category entities."""

    async def insert(self, category: Category) -> None:
        """Insert a new category. Raises ConflictError on (tenant, name) collision."""
        ...

    async def get(self, tenant_id: str, category_id: str) -> Category | None:
        """Lookup by id, tenant-scoped. None if missing or wrong tenant."""
        ...

    async def get_by_name(self, tenant_id: str, name: str) -> Category | None:
        """Lookup by canonical (trimmed) name, for upsert flows."""
        ...

    async def list_by_tenant(
        self,
        tenant_id: str,
        *,
        include_archived: bool = False,
        limit: int = 100,
        cursor: str | None = None,
    ) -> tuple[list[Category], str | None]:
        """Cursor-paginated list."""
        ...

    async def update(
        self,
        tenant_id: str,
        category_id: str,
        *,
        description: str | None = None,
        keywords: list[str] | None = None,
        archived: bool | None = None,
    ) -> Category | None:
        """Partial update. Only the fields explicitly passed are changed."""
        ...

    async def count_by_tenant(self, tenant_id: str) -> int:
        """For UI summary widgets and quota checks."""
        ...
