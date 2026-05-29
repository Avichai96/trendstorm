"""Sources resource — register and manage data sources."""

from __future__ import annotations

from trendstorm_shared.models import SourceListResponse, SourceResponse
from trendstorm_shared.types import SourceType

from ._base import AsyncAPIResource


class SourcesResource(AsyncAPIResource):
    """Manage data sources (URLs, RSS feeds, APIs, sitemaps).

    Examples::

        src = await ts.sources.add(
            category_id=cat.id,
            url="https://example.com/feed.rss",
            type=SourceType.RSS,
        )
        sources = await ts.sources.list(category_id=cat.id)
        source = await ts.sources.get(src.id)
    """

    async def add(
        self,
        *,
        category_id: str,
        url: str,
        label: str | None = None,
        type: SourceType = SourceType.HTTP,
    ) -> SourceResponse:
        """Register a single source URL."""
        body: dict = {"category_id": category_id, "url": url, "type": type}
        if label is not None:
            body["label"] = label
        data = await self._post("/v1/sources", body)
        return SourceResponse.model_validate(data)

    async def add_many(
        self,
        *,
        category_id: str,
        urls: list[str],
        type: SourceType = SourceType.HTTP,
    ) -> list[SourceResponse]:
        """Register multiple source URLs sequentially. Returns all created sources."""
        return [await self.add(category_id=category_id, url=url, type=type) for url in urls]

    async def get(self, source_id: str) -> SourceResponse:
        """Fetch a single source by ID."""
        data = await self._get(f"/v1/sources/{source_id}")
        return SourceResponse.model_validate(data)

    async def list(
        self,
        *,
        category_id: str,
        enabled_only: bool = False,
    ) -> SourceListResponse:
        """List all sources for a category."""
        data = await self._get(
            "/v1/sources",
            category_id=category_id,
            enabled_only=enabled_only,
        )
        return SourceListResponse.model_validate(data)

    async def remove(self, source_id: str) -> None:
        """Disable a source (soft-delete via PATCH)."""
        await self._patch(f"/v1/sources/{source_id}", {"enabled": False})
