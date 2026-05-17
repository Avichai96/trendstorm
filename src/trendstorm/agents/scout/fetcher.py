"""Async HTTP fetcher with per-host Redis token-bucket rate limiting.

One Fetcher instance is shared across all concurrent tasks in a worker.
The rate limiter is keyed by (tenant_id, host) so different tenants don't
compete, and different hosts within the same tenant are budgeted separately.

Concurrency note: the Fetcher handles one URL at a time. The surrounding
pipeline (pipeline.py, Phase 6) is responsible for the Semaphore +
producer-consumer queue that limits simultaneous in-flight requests per job.
Never call fetch() inside a bare asyncio.gather over an unbounded source list.

Usage:
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(settings.ingest.fetch_timeout_seconds),
        headers={"User-Agent": settings.ingest.user_agent},
        max_redirects=settings.ingest.max_redirects,
    ) as http:
        rate_limiter = RateLimiter(
            redis=redis_client.client,
            rate=settings.ingest.rate_limit_rate,
            burst=settings.ingest.rate_limit_burst,
        )
        fetcher = Fetcher(
            client=http,
            rate_limiter=rate_limiter,
            max_response_bytes=settings.ingest.max_response_bytes,
        )
        result = await fetcher.fetch(url, source_id=src.id, tenant_id=tenant_id)
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx

from trendstorm.domain.documents.models import FetchMetadata
from trendstorm.shared.errors import FetchError, HostRateLimitedError
from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    from trendstorm.agents.scout.rate_limit import RateLimiter

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Intermediate result from a single HTTP fetch, before parsing."""

    source_id: str
    url: str            # final URL after redirects
    raw_bytes: bytes
    content_type: str   # bare MIME type (params stripped) for parser routing
    encoding: str | None
    metadata: FetchMetadata


class Fetcher:
    """Async HTTP fetcher with rate limiting and structured error reporting.

    A single instance is safe to use concurrently from multiple tasks — both
    httpx.AsyncClient and RateLimiter are thread/task safe. The caller
    controls concurrency via a Semaphore outside this class.
    """

    # Content-types we'll pass to parsers. Anything else (PDFs, images, binary)
    # is rejected early rather than downloading bytes we can't use.
    _ACCEPTED_MIME_TYPES: frozenset[str] = frozenset({
        "text/html",
        "application/xhtml+xml",
        "application/xml",
        "text/xml",
        "application/rss+xml",
        "application/atom+xml",
        "application/json",
        "text/plain",
    })

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        rate_limiter: RateLimiter,
        max_response_bytes: int = 10 * 1024 * 1024,
    ) -> None:
        self._client = client
        self._rate_limiter = rate_limiter
        self._max_response_bytes = max_response_bytes

    async def fetch(
        self,
        url: str,
        *,
        source_id: str,
        tenant_id: str,
    ) -> FetchResult:
        """Fetch a single URL, applying token-bucket rate limiting first.

        Raises:
            HostRateLimitedError: our own bucket is exhausted for this host.
                Caller should back off by wait_ms before retrying.
            FetchError: HTTP error (4xx/5xx), network failure, timeout,
                oversized response, or unacceptable content-type.

        """
        host = urlsplit(url).hostname or url
        allowed, wait_ms = await self._rate_limiter.acquire(tenant_id, host)
        if not allowed:
            raise HostRateLimitedError(
                f"Rate limit for {host!r} exceeded; retry after {wait_ms} ms",
                context={"host": host, "wait_ms": wait_ms, "source_id": source_id},
            )

        t_start = time.perf_counter()
        try:
            response = await self._client.get(url, follow_redirects=True)
        except httpx.TimeoutException as exc:
            raise FetchError(
                f"Fetch timed out for {url!r}",
                context={"source_id": source_id, "url": url, "error": str(exc)},
            ) from exc
        except httpx.RequestError as exc:
            raise FetchError(
                f"Network error fetching {url!r}: {exc}",
                context={"source_id": source_id, "url": url, "error": str(exc)},
            ) from exc

        duration_ms = int((time.perf_counter() - t_start) * 1000)
        final_url = str(response.url)

        if response.status_code >= 400:
            raise FetchError(
                f"HTTP {response.status_code} fetching {url!r}",
                context={
                    "source_id": source_id,
                    "url": url,
                    "final_url": final_url,
                    "http_status": response.status_code,
                },
            )

        # Read content — httpx buffers the whole response; check size before
        # handing raw bytes upstream to avoid OOM on unexpectedly large pages.
        raw_bytes = response.content
        if len(raw_bytes) > self._max_response_bytes:
            raise FetchError(
                f"Response too large: {len(raw_bytes):,} bytes (limit {self._max_response_bytes:,})",
                context={
                    "source_id": source_id,
                    "url": url,
                    "bytes": len(raw_bytes),
                    "limit": self._max_response_bytes,
                },
            )

        raw_content_type = response.headers.get("content-type", "text/html")
        mime_type = raw_content_type.split(";")[0].strip().lower()

        if mime_type not in self._ACCEPTED_MIME_TYPES:
            raise FetchError(
                f"Unacceptable content-type {mime_type!r} for {url!r}",
                context={
                    "source_id": source_id,
                    "url": url,
                    "content_type": mime_type,
                },
            )

        logger.debug(
            "fetch_ok",
            source_id=source_id,
            url=url,
            final_url=final_url,
            http_status=response.status_code,
            bytes=len(raw_bytes),
            duration_ms=duration_ms,
            content_type=mime_type,
        )

        return FetchResult(
            source_id=source_id,
            url=final_url,
            raw_bytes=raw_bytes,
            content_type=mime_type,
            encoding=response.encoding,
            metadata=FetchMetadata(
                http_status=response.status_code,
                content_type=raw_content_type,
                bytes_fetched=len(raw_bytes),
                final_url=final_url,
                fetch_duration_ms=duration_ms,
            ),
        )
