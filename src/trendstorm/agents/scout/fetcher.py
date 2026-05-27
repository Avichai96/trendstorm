"""Async HTTP fetcher with SSRF validation, rate limiting, and manual redirect following.

One Fetcher instance is shared across all concurrent tasks in a worker.
The rate limiter is keyed by (tenant_id, host) so different tenants don't
compete, and different hosts within the same tenant are budgeted separately.

SSRF protection:
    Every URL -- initial and each redirect hop -- is validated by
    infrastructure/security/ssrf.py before any network connection is made.
    The global blocklist and optional per-tenant blocklist are checked first.
    Redirects are followed manually (not via httpx follow_redirects=True) so
    each hop can be independently validated. Max 3 redirect hops (SSRF
    constant, harder than the config max_redirects if that is higher).
    Scheme downgrade (https -> http) is blocked on all redirect hops.

Concurrency note: the Fetcher handles one URL at a time. The surrounding
pipeline (pipeline.py, Phase 6) is responsible for the Semaphore +
producer-consumer queue that limits simultaneous in-flight requests per job.
Never call fetch() inside a bare asyncio.gather over an unbounded source list.

Usage:
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(settings.ingest.fetch_timeout_seconds),
        headers={"User-Agent": settings.ingest.user_agent},
        follow_redirects=False,   # SSRF layer handles redirects manually
    ) as http:
        fetcher = Fetcher(
            client=http,
            rate_limiter=rate_limiter,
            max_response_bytes=settings.ingest.max_response_bytes,
        )
        result = await fetcher.fetch(url, source_id=src.id, tenant_id=tenant_id)
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx
from opentelemetry import trace

from trendstorm.domain.documents.models import FetchMetadata
from trendstorm.infrastructure.security.blocklist import (
    check_global_blocklist,
    check_tenant_blocklist,
)
from trendstorm.infrastructure.security.ssrf import (
    MAX_REDIRECTS,
    validate_redirect,
    validate_url,
)
from trendstorm.shared.errors import FetchError, HostRateLimitedError, SSRFBlockedError
from trendstorm.shared.logging import get_logger
from trendstorm.shared.metrics.registry import record_security_block
from trendstorm.shared.tracing.semantics import Attr

if TYPE_CHECKING:
    from trendstorm.agents.scout.rate_limit import RateLimiter
    from trendstorm.domain.audit_log.repository import AuditLogRepository
    from trendstorm.domain.url_blocklists.repository import UrlBlocklistRepository

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)


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
    """Async HTTP fetcher with SSRF validation, rate limiting, and manual redirect following.

    A single instance is safe to use concurrently from multiple tasks -- both
    httpx.AsyncClient and RateLimiter are thread/task safe. The caller
    controls concurrency via a Semaphore outside this class.

    Args:
        client: httpx.AsyncClient configured with follow_redirects=False.
        rate_limiter: Per-(tenant, host) token bucket.
        max_response_bytes: Reject responses larger than this.
        blocklist_repo: Optional per-tenant blocklist repository. When None,
            only the global blocklist and SSRF IP rules are enforced.
        audit_log_repo: Optional audit log repository. When None, block events
            are logged via structlog only (still metric-counted).
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
        blocklist_repo: UrlBlocklistRepository | None = None,
        audit_log_repo: AuditLogRepository | None = None,
    ) -> None:
        self._client = client
        self._rate_limiter = rate_limiter
        self._max_response_bytes = max_response_bytes
        self._blocklist_repo = blocklist_repo
        self._audit_log_repo = audit_log_repo

    async def fetch(
        self,
        url: str,
        *,
        source_id: str,
        tenant_id: str,
    ) -> FetchResult:
        """Fetch a single URL with SSRF validation on every hop.

        Raises:
            SSRFBlockedError: URL or redirect target is blocked by SSRF rules.
            HostRateLimitedError: Our own bucket is exhausted for this host.
            FetchError: HTTP error (4xx/5xx), network failure, timeout,
                oversized response, or unacceptable content-type.
        """
        # Pre-flight SSRF validation on the initial URL
        await self._validate_and_audit(url, tenant_id=tenant_id, source_id=source_id)

        host = urlsplit(url).hostname or url
        allowed, wait_ms = await self._rate_limiter.acquire(tenant_id, host)
        if not allowed:
            raise HostRateLimitedError(
                f"Rate limit for {host!r} exceeded; retry after {wait_ms} ms",
                context={"host": host, "wait_ms": wait_ms, "source_id": source_id},
            )

        t_start = time.perf_counter()
        current_url = url
        hop = 0

        while True:
            with tracer.start_as_current_span(
                "scout.fetch_hop",
                attributes={
                    Attr.HTTP_URL: current_url,
                    Attr.SOURCE_ID: source_id,
                    Attr.SECURITY_REDIRECT_HOP: hop,
                },
            ):
                try:
                    response = await self._client.get(
                        current_url, follow_redirects=False
                    )
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

            if response.is_redirect:
                if hop >= MAX_REDIRECTS:
                    raise SSRFBlockedError(
                        f"Exceeded max redirects ({MAX_REDIRECTS}) for {url!r}",
                        reason="ssrf_max_redirects",
                        url=current_url,
                    )
                location = response.headers.get("location", "")
                if not location:
                    raise FetchError(
                        f"Redirect with no Location header from {current_url!r}",
                        context={"source_id": source_id},
                    )

                # Resolve relative Location headers against current URL
                next_url = str(httpx.URL(current_url).copy_with()) if not location else location
                if location.startswith("/"):
                    parsed = urlsplit(current_url)
                    next_url = f"{parsed.scheme}://{parsed.netloc}{location}"
                elif not location.startswith("http"):
                    next_url = location
                else:
                    next_url = location

                await self._validate_redirect_and_audit(
                    from_url=current_url,
                    to_url=next_url,
                    tenant_id=tenant_id,
                    source_id=source_id,
                )

                current_url = next_url
                hop += 1
                continue

            # Not a redirect -- process the final response
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
                redirect_hops=hop,
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

    # ------------------------------------------------------------------
    # Private validation helpers
    # ------------------------------------------------------------------

    async def _validate_and_audit(
        self, url: str, *, tenant_id: str, source_id: str
    ) -> None:
        """Run SSRF + blocklist checks on url; audit and metric on block."""
        hostname = (urlsplit(url).hostname or "").lower()
        try:
            check_global_blocklist(hostname, url)
            await asyncio.get_event_loop().run_in_executor(
                None, validate_url, url
            )
            if self._blocklist_repo is not None:
                await check_tenant_blocklist(
                    hostname, url,
                    tenant_id=tenant_id,
                    repo=self._blocklist_repo,
                )
        except SSRFBlockedError as exc:
            await self._handle_ssrf_block(exc, tenant_id=tenant_id, source_id=source_id)
            raise

    async def _validate_redirect_and_audit(
        self, from_url: str, to_url: str, *, tenant_id: str, source_id: str
    ) -> None:
        """Validate a redirect hop; audit and metric on block."""
        hostname = (urlsplit(to_url).hostname or "").lower()
        try:
            check_global_blocklist(hostname, to_url)
            await asyncio.get_event_loop().run_in_executor(
                None, validate_redirect, from_url, to_url
            )
            if self._blocklist_repo is not None:
                await check_tenant_blocklist(
                    hostname, to_url,
                    tenant_id=tenant_id,
                    repo=self._blocklist_repo,
                )
        except SSRFBlockedError as exc:
            await self._handle_ssrf_block(exc, tenant_id=tenant_id, source_id=source_id)
            raise

    async def _handle_ssrf_block(
        self, exc: SSRFBlockedError, *, tenant_id: str, source_id: str
    ) -> None:
        """Increment security metric and write audit log entry on SSRF block."""
        record_security_block(exc.reason, tenant_id)
        logger.warning(
            "ssrf_blocked",
            reason=exc.reason,
            url=exc.url,
            source_id=source_id,
            tenant_id=tenant_id,
        )
        if self._audit_log_repo is not None:
            from trendstorm.domain.audit_log.models import AuditLogEntry
            entry = AuditLogEntry(
                tenant_id=tenant_id,
                event_type="ssrf_blocked",
                actor="system",
                resource_type="source",
                resource_id=source_id,
                action="validate_url",
                outcome="blocked",
                metadata={"reason": exc.reason, "url": exc.url},
            )
            await self._audit_log_repo.append(entry)
