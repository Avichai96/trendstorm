"""Error hierarchy for the TrendStorm SDK.

All errors raised by SDK methods are subclasses of TrendStormError so callers
can catch the entire hierarchy with a single except clause.

    TrendStormError
    └── APIError            server returned an HTTP error response
        ├── RateLimited     429
        ├── NotFound        404
        ├── Unauthorized    401 / 403
        ├── ValidationError 422
        └── ServerError     5xx

Non-API errors (network timeout, bad config, SSE stream issues):
    TrendStormError
    ├── ConfigurationError  bad constructor args, missing env vars
    ├── StreamError         SSE stream parse or connection error
    └── HeartbeatTimeout    no event received within heartbeat window
"""

from __future__ import annotations

import email.utils
import time
from typing import Any


def _parse_retry_after_header(headers: dict[str, str]) -> float | None:
    """Parse Retry-After header value to seconds from now.

    Accepts both integer seconds ("30") and HTTP-date formats.
    Returns None if the header is absent or unparseable.
    """
    value = headers.get("retry-after") or headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        return max(0.0, parsed.timestamp() - time.time())
    except Exception:
        return None


class TrendStormError(Exception):
    """Base class for all SDK errors."""


class ConfigurationError(TrendStormError):
    """Raised when the client is misconfigured (missing key, bad URL, etc.)."""


class StreamError(TrendStormError):
    """Raised on SSE parse failure or unrecoverable connection drop."""


class HeartbeatTimeout(TrendStormError):
    """No SSE event received within the configured heartbeat window."""

    def __init__(self, timeout_seconds: float) -> None:
        super().__init__(f"No event received in {timeout_seconds}s")
        self.timeout_seconds = timeout_seconds


class APIError(TrendStormError):
    """Server returned an HTTP error response.

    Attributes:
        status_code:    HTTP status code (e.g. 404).
        error_code:     server-side error code string (e.g. "not_found").
        message:        human-readable error message from the server.
        request_id:     value of X-Request-ID response header, if present.
        correlation_id: value of X-Correlation-ID response header, if present.
        raw:            full parsed response body dict.
    """

    def __init__(
        self,
        *,
        status_code: int,
        error_code: str = "unknown",
        message: str = "",
        request_id: str | None = None,
        correlation_id: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"HTTP {status_code}: {error_code} — {message}")
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.request_id = request_id
        self.correlation_id = correlation_id
        self.raw = raw or {}

    @classmethod
    def from_response(
        cls, status_code: int, body: dict[str, Any], headers: dict[str, str]
    ) -> "APIError":
        err = body.get("error", {})
        klass = _STATUS_TO_CLASS.get(status_code, APIError)
        base_kwargs: dict[str, Any] = {
            "status_code": status_code,
            "error_code": err.get("code", "unknown"),
            "message": err.get("message", str(body)),
            "request_id": headers.get("x-request-id"),
            "correlation_id": body.get("correlation_id"),
            "raw": body,
        }
        if klass is RateLimited:
            return RateLimited(retry_after=_parse_retry_after_header(headers), **base_kwargs)
        return klass(**base_kwargs)


class RateLimited(APIError):
    """429 Too Many Requests. Check ``retry_after`` for the suggested delay."""

    def __init__(self, *, retry_after: float | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.retry_after = retry_after


class NotFound(APIError):
    """404 Not Found."""


class Unauthorized(APIError):
    """401 Unauthorized or 403 Forbidden."""


class ValidationError(APIError):
    """422 Unprocessable Entity — request body failed server-side validation."""


class ServerError(APIError):
    """5xx error from the TrendStorm server."""


_STATUS_TO_CLASS: dict[int, type[APIError]] = {
    404: NotFound,
    401: Unauthorized,
    403: Unauthorized,
    422: ValidationError,
    429: RateLimited,
    500: ServerError,
    502: ServerError,
    503: ServerError,
    504: ServerError,
}
