"""Correlation ID middleware.

Every request gets a correlation ID:
    - If client sends `X-Correlation-ID`, we honor it (for distributed tracing
      across multiple systems).
    - Otherwise, we generate a ULID.

The ID is:
    - Bound to the logging context (visible in every log line during the request)
    - Echoed in the response header (client can correlate their logs with ours)

Why a header and not just the OTel trace_id?
    - Trace IDs are 32 hex chars — unwieldy in human contexts.
    - Many ops tools (zendesk, support emails) want a short ID to grep for.
    - OTel traces are sampled (some dropped); correlation IDs are always present.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware

from trendstorm.shared.ids import is_valid_id, new_id
from trendstorm.shared.logging import bind_context

if TYPE_CHECKING:
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp

CORRELATION_HEADER = "x-correlation-id"


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Inject a correlation ID into every request."""

    def __init__(self, app: ASGIApp, header_name: str = CORRELATION_HEADER) -> None:
        super().__init__(app)
        self._header_name = header_name

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Accept incoming ID only if it looks valid (ULID); else generate fresh.
        incoming = request.headers.get(self._header_name)
        cid = incoming if incoming and is_valid_id(incoming) else new_id()

        # Bind to logging context for the duration of this async task.
        bind_context(correlation_id=cid)

        # Also make it available on request.state for handlers that want it.
        request.state.correlation_id = cid

        response: Response = await call_next(request)
        response.headers[self._header_name] = cid
        return response
