"""Authentication strategies for the TrendStorm SDK.

Supported:
  - API key  (Authorization: Bearer ts_live_... / ts_test_...)
  - OAuth 2.0 Bearer token with optional auto-refresh

The auth object is injected into each HTTP request by ``TrendStormClient._request``.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


class _Auth(ABC):
    @abstractmethod
    def headers(self) -> dict[str, str]: ...

    async def refresh(self) -> None:
        """Called before each request. Override to implement token refresh."""


class ApiKeyAuth(_Auth):
    """Static API key authentication. Key never changes after construction."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}


class OAuthAuth(_Auth):
    """OAuth 2.0 Bearer token with optional auto-refresh.

    If ``refresh_token`` is provided, the client will attempt to obtain a new
    access token when the current one expires (checked on each request).
    Refresh happens at most once concurrently — parallel requests block on the
    same refresh coroutine via ``asyncio.Lock``.
    """

    def __init__(
        self,
        access_token: str,
        refresh_token: str | None = None,
        token_url: str | None = None,
        expires_at: float | None = None,
    ) -> None:
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._token_url = token_url
        self._expires_at = expires_at
        self._lock = asyncio.Lock()

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    async def refresh(self) -> None:
        if not self._refresh_token or not self._token_url:
            return
        if self._expires_at is None or time.time() < self._expires_at - 30:
            return
        async with self._lock:
            if time.time() < (self._expires_at or 0) - 30:
                return  # another coroutine refreshed while we waited
            await self._do_refresh()

    async def _do_refresh(self) -> None:
        import httpx

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self._token_url,  # type: ignore[arg-type]
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            self._access_token = data["access_token"]
            if "refresh_token" in data:
                self._refresh_token = data["refresh_token"]
            if "expires_in" in data:
                self._expires_at = time.time() + int(data["expires_in"])
