"""Categories resource — CRUD operations on trend categories."""
from __future__ import annotations

from trendstorm_shared.models import (
    CategoryListResponse,
    CategoryResponse,
)

from ._base import AsyncAPIResource


class CategoriesResource(AsyncAPIResource):
    """Manage trend categories.

    Examples::

        cat = await ts.categories.create(name="AI Safety", keywords=["alignment"])
        cats = await ts.categories.list()
        cat = await ts.categories.get(cat.id)
        cat = await ts.categories.update(cat.id, archived=True)
    """

    async def create(
        self,
        *,
        name: str,
        description: str | None = None,
        keywords: list[str] | None = None,
    ) -> CategoryResponse:
        """Create a new trend category. Returns the created ``CategoryResponse``."""
        body: dict = {"name": name}
        if description is not None:
            body["description"] = description
        if keywords is not None:
            body["keywords"] = keywords
        data = await self._post("/v1/categories", body)
        return CategoryResponse.model_validate(data)

    async def get(self, category_id: str) -> CategoryResponse:
        """Fetch a single category by ID."""
        data = await self._get(f"/v1/categories/{category_id}")
        return CategoryResponse.model_validate(data)

    async def update(
        self,
        category_id: str,
        *,
        description: str | None = None,
        keywords: list[str] | None = None,
        archived: bool | None = None,
    ) -> CategoryResponse:
        """Partial-update a category. Only non-None fields are sent."""
        body: dict = {}
        if description is not None:
            body["description"] = description
        if keywords is not None:
            body["keywords"] = keywords
        if archived is not None:
            body["archived"] = archived
        data = await self._patch(f"/v1/categories/{category_id}", body)
        return CategoryResponse.model_validate(data)

    async def list(
        self,
        *,
        include_archived: bool = False,
        limit: int = 100,
        cursor: str | None = None,
    ) -> CategoryListResponse:
        """List all categories for the tenant, cursor-paginated."""
        data = await self._get(
            "/v1/categories",
            include_archived=include_archived,
            limit=limit,
            cursor=cursor,
        )
        return CategoryListResponse.model_validate(data)
