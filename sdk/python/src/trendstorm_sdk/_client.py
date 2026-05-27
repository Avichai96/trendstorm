"""TrendStormClient — async HTTP client for the TrendStorm AI API.

Typical usage::

    async with TrendStormClient(api_key="ts_live_...") as ts:
        category = await ts.categories.create(name="AI Safety", keywords=["alignment"])
        job = await ts.jobs.create(category_id=category.id)
        async for event in ts.jobs.stream(job.job_id):
            print(event.event_type, event.payload)

Environment variables:
    TRENDSTORM_API_KEY    — used when ``api_key`` argument is omitted.
    TRENDSTORM_BASE_URL   — used when ``base_url`` argument is omitted.
    TRENDSTORM_OAUTH_TOKEN — used when ``oauth_token`` argument is omitted.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

from ._auth import ApiKeyAuth, OAuthAuth, _Auth
from ._errors import APIError, ConfigurationError
from ._retry import retry_request
from .resources.api_keys import ApiKeysResource
from .resources.categories import CategoriesResource
from .resources.jobs import JobsResource
from .resources.quota import QuotaResource
from .resources.reviews import ReviewsResource
from .resources.sources import SourcesResource

_DEFAULT_BASE_URL = "https://api.trendstorm.io"
_DEFAULT_TIMEOUT = 30.0
_DEFAULT_MAX_RETRIES = 5


class TrendStormClient:
    """Async HTTP client for the TrendStorm AI REST API.

    Must be used as an async context manager so the underlying httpx client
    is properly initialised and torn down::

        async with TrendStormClient(api_key="ts_live_...") as ts:
            ...

    Args:
        api_key:              TrendStorm API key (``ts_live_*`` or ``ts_test_*``).
                              Falls back to ``TRENDSTORM_API_KEY`` env var.
        base_url:             API base URL. Falls back to ``TRENDSTORM_BASE_URL``
                              or the production endpoint.
        timeout:              HTTP request timeout in seconds (default 30).
        max_retries:          Max retry attempts for 429 / 5xx (default 5).
        oauth_token:          OAuth 2.0 access token — alternative to API key.
                              Falls back to ``TRENDSTORM_OAUTH_TOKEN`` env var.
        oauth_refresh_token:  Refresh token for automatic OAuth renewal.
        oauth_token_url:      Token endpoint for OAuth refresh.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        oauth_token: str | None = None,
        oauth_refresh_token: str | None = None,
        oauth_token_url: str | None = None,
    ) -> None:
        resolved_key = api_key or os.environ.get("TRENDSTORM_API_KEY")
        resolved_token = oauth_token or os.environ.get("TRENDSTORM_OAUTH_TOKEN")

        if resolved_key:
            self._auth: _Auth = ApiKeyAuth(resolved_key)
        elif resolved_token:
            self._auth = OAuthAuth(
                resolved_token,
                oauth_refresh_token,
                oauth_token_url,
            )
        else:
            raise ConfigurationError(
                "No API key or OAuth token provided. "
                "Pass api_key= or set the TRENDSTORM_API_KEY environment variable."
            )

        self._base_url = (base_url or os.environ.get("TRENDSTORM_BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._http: httpx.AsyncClient | None = None

        self._categories = CategoriesResource(self)
        self._sources = SourcesResource(self)
        self._jobs = JobsResource(self)
        self._reviews = ReviewsResource(self)
        self._quota = QuotaResource(self)
        self._api_keys = ApiKeysResource(self)

    async def __aenter__(self) -> "TrendStormClient":
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(self._timeout),
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    @property
    def categories(self) -> CategoriesResource:
        return self._categories

    @property
    def sources(self) -> SourcesResource:
        return self._sources

    @property
    def jobs(self) -> JobsResource:
        return self._jobs

    @property
    def reviews(self) -> ReviewsResource:
        return self._reviews

    @property
    def quota(self) -> QuotaResource:
        return self._quota

    @property
    def api_keys(self) -> ApiKeysResource:
        return self._api_keys

    # ------------------------------------------------------------------
    # Internal request helpers — used by resource classes only
    # ------------------------------------------------------------------

    def _http_client(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError(
                "TrendStormClient is not open. Use 'async with TrendStormClient(...) as ts:'."
            )
        return self._http

    def _auth_headers(self) -> dict[str, str]:
        return self._auth.headers()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self._auth.refresh()
        client = self._http_client()
        headers = self._auth_headers()

        def _make_request() -> Any:
            return client.request(
                method,
                path,
                headers=headers,
                params={k: v for k, v in (params or {}).items() if v is not None},
                json=json,
            )

        response = await retry_request(_make_request, max_retries=self._max_retries, method=method)
        return self._parse_response(response)

    def _parse_response(self, response: httpx.Response) -> dict[str, Any]:
        if response.status_code == 204:
            return {}
        if response.is_success:
            return response.json()
        try:
            body = response.json()
        except Exception:
            body = {"error": {"code": "parse_error", "message": response.text[:500]}}
        raise APIError.from_response(
            response.status_code,
            body,
            dict(response.headers),
        )
