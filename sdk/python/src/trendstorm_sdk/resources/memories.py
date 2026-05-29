"""Memories resource — long-term memory management for categories.

Requires API key with the ``tenant_admin`` role.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ._base import AsyncAPIResource


class MemoryRecord(BaseModel):
    """A long-term memory associated with a category."""

    model_config = ConfigDict(extra="ignore")

    id: str
    tenant_id: str
    category_id: str
    kind: str
    source: str
    content: str
    confidence: float
    is_active: bool
    tags: list[str] = Field(default_factory=list)
    superseded_by: str | None = None
    created_at: str
    updated_at: str


class MemoriesResource(AsyncAPIResource):
    """Manage user-curated long-term memories for a category.

    Episodic and semantic memories are created automatically by the pipeline.
    This resource lets ``tenant_admin`` users create, list, and deactivate
    memories — useful for injecting known facts before initial data is available.

    All methods require an API key with the ``tenant_admin`` role.

    Examples::

        # Create a user-curated memory
        memory = await ts.memories.create(
            category_id="01HV...",
            content="OpenAI released GPT-5 in Q2 2025 with reasoning capabilities.",
            curated_by="alice@example.com",
        )

        # List active memories for a category
        memories = await ts.memories.list(category_id="01HV...")
        for m in memories:
            print(m.kind, m.content[:80])

        # Deactivate (soft-delete) a memory
        await ts.memories.delete(category_id="01HV...", memory_id=memory.id)
    """

    async def create(
        self,
        category_id: str,
        *,
        content: str,
        curated_by: str,
        confidence: float = 1.0,
        tags: list[str] | None = None,
    ) -> MemoryRecord:
        """Create a user-curated memory for the category."""
        data = await self._post(
            f"/v1/categories/{category_id}/memories",
            {
                "content": content,
                "curated_by": curated_by,
                "confidence": confidence,
                "tags": tags or [],
            },
        )
        return MemoryRecord.model_validate(data)

    async def list(self, category_id: str) -> list[MemoryRecord]:
        """List all active memories for the category."""
        data = await self._get(f"/v1/categories/{category_id}/memories")
        return [MemoryRecord.model_validate(item) for item in data.get("items", [])]

    async def delete(self, category_id: str, memory_id: str) -> None:
        """Deactivate (soft-delete) a memory by ID."""
        await self._client._request(
            "DELETE",
            f"/v1/categories/{category_id}/memories/{memory_id}",
        )
