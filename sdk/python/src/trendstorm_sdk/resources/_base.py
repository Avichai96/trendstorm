"""Base class for all resource objects."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .._client import TrendStormClient


class AsyncAPIResource:
    """Thin wrapper around ``TrendStormClient._request`` for a given API surface."""

    def __init__(self, client: "TrendStormClient") -> None:
        self._client = client

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        return await self._client._request("GET", path, params=params or None)

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._client._request("POST", path, json=body)

    async def _patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._client._request("PATCH", path, json=body)

    async def _delete(self, path: str) -> dict[str, Any]:
        return await self._client._request("DELETE", path)
