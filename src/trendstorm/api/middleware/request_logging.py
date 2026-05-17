"""Request logging middleware.

Emits one structured log per request with method, path, status, duration.

Why not use uvicorn's access log?
    - uvicorn's access log is unstructured and uses its own format.
    - We want it correlated with our trace_id, correlation_id, tenant_id.
    - We want exception logging in the same format.

Convention:
    - Log AFTER the response is generated (so we have status + duration).
    - Log at INFO for 2xx/3xx, WARNING for 4xx, ERROR for 5xx.
    - Log unhandled exceptions before they propagate to the exception handler.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp


logger = get_logger(__name__)

# Skip noise paths from the access log (probes, metrics).
_QUIET_PATHS = frozenset({"/health/live", "/health/ready", "/metrics"})


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """One structured log line per request."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # time.perf_counter() is monotonic; safe for measuring durations
        start = time.perf_counter()
        path = request.url.path
        method = request.method

        try:
            response = await call_next(request)
        except Exception as exc:
            duration_ms = (time.perf_counter() - start) * 1000
            logger.exception(
                "request_failed",
                method=method,
                path=path,
                duration_ms=round(duration_ms, 2),
                error_type=type(exc).__name__,
            )
            raise

        duration_ms = (time.perf_counter() - start) * 1000
        status = response.status_code

        if path in _QUIET_PATHS:
            return response  # don't log probes

        log_kwargs = {
            "method": method,
            "path": path,
            "status_code": status,
            "duration_ms": round(duration_ms, 2),
        }
        if status >= 500:
            logger.error("request_completed", **log_kwargs)
        elif status >= 400:
            logger.warning("request_completed", **log_kwargs)
        else:
            logger.info("request_completed", **log_kwargs)

        return response
