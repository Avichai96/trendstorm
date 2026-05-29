"""MemoryRepository Protocol — long-term memory persistence contract."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from trendstorm.domain.memories.models import Memory, MemoryKind


@runtime_checkable
class MemoryRepository(Protocol):
    """Persistence contract for long-term memories.

    All queries are tenant-scoped. Implementations must enforce tenant_id
    on every query (the Mongo implementation does this via _tenant_query).
    """

    async def insert(self, memory: Memory) -> None:
        """Persist a new memory document."""
        ...

    async def get(self, tenant_id: str, memory_id: str) -> Memory | None:
        """Fetch a single memory by id, scoped to tenant."""
        ...

    async def list_active_for_category(
        self,
        tenant_id: str,
        category_id: str,
        *,
        kind: MemoryKind | None = None,
        limit: int = 50,
    ) -> list[Memory]:
        """Return active (non-superseded) memories for a category, newest first."""
        ...

    async def list_for_category(
        self,
        tenant_id: str,
        category_id: str,
        *,
        kind: MemoryKind | None = None,
        limit: int = 50,
        before_id: str | None = None,
    ) -> list[Memory]:
        """Paginated list (all statuses) for a category, newest first."""
        ...

    async def set_embedding_id(
        self,
        tenant_id: str,
        memory_id: str,
        embedding_id: str,
    ) -> None:
        """Record the ChromaDB vector ID after upsert."""
        ...

    async def supersede(
        self,
        tenant_id: str,
        old_memory_id: str,
        superseded_by_id: str,
    ) -> None:
        """Mark old_memory_id as superseded by superseded_by_id."""
        ...

    async def deactivate(self, tenant_id: str, memory_id: str) -> None:
        """Soft-delete (set is_active=False). Used by the DELETE API endpoint."""
        ...
