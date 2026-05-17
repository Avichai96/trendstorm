"""Tenant context middleware (legacy shim).

In AUTH_MODE=header and AUTH_MODE=disabled, this middleware is the source of
tenant_id (read from X-Tenant-ID header). In all other modes, AuthMiddleware
already sets `request.state.tenant_id` from the authenticated credential —
this middleware is a no-op for those modes.

Why keep this at all?
  The existing routes read `request.state.tenant_id`. Auth middleware sets it
  as `ctx.tenant_id`, so in auth-enabled modes this middleware is redundant but
  harmless. In legacy modes it's still the primary setter.

  This file will be removed in Phase 13 once AUTH_MODE=header is retired.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse

from trendstorm.shared.ids import is_valid_id
from trendstorm.shared.logging import bind_context

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp


TENANT_HEADER = "x-tenant-id"

_PUBLIC_PATHS = frozenset({
    "/health/live", "/health/ready",
    "/docs", "/openapi.json", "/redoc",
    "/metrics",
})


class TenantMiddleware(BaseHTTPMiddleware):
    """If tenant_id is not already set by AuthMiddleware, read from X-Tenant-ID."""

    def __init__(self, app: ASGIApp, header_name: str = TENANT_HEADER) -> None:
        super().__init__(app)
        self._header_name = header_name

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        # If AuthMiddleware already set tenant_id, we're done.
        if getattr(request.state, "tenant_id", None):
            return await call_next(request)

        # Legacy path: read from header.
        tenant_id = request.headers.get(self._header_name)
        if not tenant_id or not is_valid_id(tenant_id):
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "missing_tenant",
                        "message": f"Header {self._header_name!r} required (valid ULID).",
                    }
                },
            )

        bind_context(tenant_id=tenant_id)
        request.state.tenant_id = tenant_id
        return await call_next(request)
