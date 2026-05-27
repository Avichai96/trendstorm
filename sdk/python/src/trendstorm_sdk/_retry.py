"""Retry / backoff logic for the TrendStorm SDK.

Strategy:
  - 429 → honour Retry-After header (seconds or HTTP-date). Fall back to
    exponential backoff if header absent.
  - 5xx → exponential backoff (1s, 2s, 4s … cap 60s).
  - 4xx (except 429) → raise immediately, no retry.
  - Network errors (ConnectError, ReadTimeout) → retry with backoff.

``retry_request`` is an async function that wraps any ``httpx.AsyncClient``
request call. Resources call it instead of calling ``client.request`` directly.
"""
from __future__ import annotations

import asyncio
import email.utils
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_BACKOFF = 60.0

T = TypeVar("T")


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Return the retry delay in seconds from Retry-After header, or None."""
    header = response.headers.get("retry-after")
    if header is None:
        return None
    # Try integer seconds first.
    try:
        return float(header)
    except ValueError:
        pass
    # Try HTTP-date format.
    try:
        parsed = email.utils.parsedate_to_datetime(header)
        delay = parsed.timestamp() - time.time()
        return max(0.0, delay)
    except Exception:
        return None


def _backoff(attempt: int) -> float:
    """Exponential backoff: 1, 2, 4, 8 … capped at MAX_BACKOFF seconds."""
    return min(_MAX_BACKOFF, 2.0 ** attempt)


async def retry_request(
    fn: Callable[[], Awaitable[httpx.Response]],
    *,
    max_retries: int = 5,
    method: str = "GET",
) -> httpx.Response:
    """Call ``fn`` and retry on transient failures.

    Args:
        fn:          Zero-argument coroutine that issues one HTTP request.
        max_retries: Maximum number of additional attempts after the first.
        method:      HTTP method (used for logging only).

    Returns:
        The first successful (non-retryable) ``httpx.Response``.

    Raises:
        httpx.HTTPError: After all retries are exhausted.
        Any non-retryable ``httpx.Response`` is returned immediately.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            response = await fn()
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
            last_exc = exc
            if attempt == max_retries:
                raise
            delay = _backoff(attempt)
            logger.debug("network error %s (attempt %d/%d) — retrying in %.1fs", exc, attempt + 1, max_retries + 1, delay)
            await asyncio.sleep(delay)
            continue

        if response.status_code not in _RETRYABLE_STATUS or attempt == max_retries:
            return response

        if response.status_code == 429:
            delay = _parse_retry_after(response) or _backoff(attempt)
        else:
            delay = _backoff(attempt)

        logger.debug(
            "%s %s → %d (attempt %d/%d) — retrying in %.1fs",
            method, response.url, response.status_code,
            attempt + 1, max_retries + 1, delay,
        )
        await asyncio.sleep(delay)

    # Should not reach here; loop always raises or returns.
    raise RuntimeError("retry_request: exhausted without returning")  # pragma: no cover
