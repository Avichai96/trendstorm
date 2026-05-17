"""Authentication middleware.

Reads AUTH_MODE from settings and dispatches to the appropriate auth strategy.
Sets `request.state.auth_context` (AuthContext) on success.

Mode semantics:
  disabled    → No auth enforced. Accepts X-Tenant-ID header as tenant_id.
                Startup fails with RuntimeError if ENV=prod and mode=disabled.
  header      → X-Tenant-ID header required. Logs deprecation warning.
                Accepts any valid ULID — no credential verification.
  key         → Bearer API key required (ts_live_* / ts_test_*).
  oauth       → Bearer JWT required (validated against configured IdP).
  key_or_oauth → Either Bearer key OR Bearer JWT accepted (production default).

All modes set request.state.auth_context so downstream handlers have a
consistent source of truth. The TenantMiddleware (tenant.py) is retired for
auth-enabled modes — auth_context.tenant_id IS the tenant.

Public paths (health, docs) bypass all auth.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse

from trendstorm.domain.auth.models import AuthContext
from trendstorm.shared.ids import is_valid_id
from trendstorm.shared.logging import bind_context, get_logger

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp

    from trendstorm.shared.config import AuthSettings, Environment

logger = get_logger(__name__)

_PUBLIC_PATHS = frozenset({
    "/health/live", "/health/ready",
    "/docs", "/openapi.json", "/redoc",
    "/metrics",
})

_BEARER_PREFIX = "bearer "
_TENANT_HEADER = "x-tenant-id"


def _bearer_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith(_BEARER_PREFIX):
        return auth[len(_BEARER_PREFIX):]
    return None


def _tenant_header(request: Request) -> str | None:
    return request.headers.get(_TENANT_HEADER)


def _make_401(message: str, code: str = "unauthorized") -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"error": {"code": code, "message": message}},
        headers={"WWW-Authenticate": "Bearer"},
    )


def _make_400(message: str, code: str = "bad_request") -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": {"code": code, "message": message}},
    )


class AuthMiddleware(BaseHTTPMiddleware):
    """Authenticate requests based on AUTH_MODE. Sets request.state.auth_context.

    AuthService is read lazily from request.app.state.auth_service because the
    Mongo client used by AuthService is connected in lifespan, after middleware
    is registered. This is the standard pattern for lifespan-dependent services
    in Starlette middleware.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        settings: AuthSettings,
        app_env: Environment,
    ) -> None:
        super().__init__(app)
        self._settings = settings
        self._env = app_env

        # Refuse to start in production with auth disabled.
        from trendstorm.shared.config import AuthMode, Environment
        if settings.mode == AuthMode.DISABLED and app_env == Environment.PROD:
            raise RuntimeError(
                "AUTH_MODE=disabled is forbidden in production (APP__ENV=prod). "
                "Set AUTH__MODE=key_or_oauth."
            )

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        from trendstorm.shared.config import AuthMode
        mode = self._settings.mode
        # Lazy read from app.state (set during lifespan startup).
        auth_svc = getattr(request.app.state, "auth_service", None)

        if mode == AuthMode.DISABLED:
            # Dev-only: extract tenant from header or fall back to a dev sentinel.
            tid = _tenant_header(request)
            if not tid or not is_valid_id(tid):
                return _make_400(
                    f"Auth disabled; provide {_TENANT_HEADER!r} header (valid ULID).",
                    code="missing_tenant",
                )
            ctx = AuthContext(tenant_id=tid, source="legacy")

        elif mode == AuthMode.HEADER:
            tid = _tenant_header(request)
            if not tid or not is_valid_id(tid):
                return _make_400(
                    f"Header {_TENANT_HEADER!r} required (valid ULID). "
                    "This auth mode is deprecated; migrate to API keys.",
                    code="missing_tenant",
                )
            logger.warning(
                "auth.legacy_header_used",
                path=request.url.path,
                tenant_id=tid,
            )
            ctx = AuthContext(tenant_id=tid, source="legacy")

        elif mode == AuthMode.KEY:
            if auth_svc is None:
                return _make_401("Auth service unavailable", code="server_error")
            token = _bearer_token(request)
            if not token:
                return _make_401("API key required (Authorization: Bearer ts_live_…)")
            try:
                ctx = await auth_svc.authenticate_by_key(token)
            except Exception as e:
                return _make_401(str(e), code="invalid_api_key")

        elif mode == AuthMode.OAUTH:
            if auth_svc is None:
                return _make_401("Auth service unavailable", code="server_error")
            token = _bearer_token(request)
            if not token:
                return _make_401("JWT required (Authorization: Bearer <token>)")
            try:
                ctx = await auth_svc.authenticate_by_jwt(token)
            except Exception as e:
                return _make_401(str(e), code="invalid_jwt")

        else:  # KEY_OR_OAUTH
            if auth_svc is None:
                return _make_401("Auth service unavailable", code="server_error")
            token = _bearer_token(request)
            if not token:
                return _make_401(
                    "Provide Authorization: Bearer ts_live_… (API key) or Bearer <JWT>"
                )
            # Detect by prefix: ts_ = API key, anything else = JWT.
            if token.startswith("ts_"):
                try:
                    ctx = await auth_svc.authenticate_by_key(token)
                except Exception as e:
                    return _make_401(str(e), code="invalid_api_key")
            else:
                try:
                    ctx = await auth_svc.authenticate_by_jwt(token)
                except Exception as e:
                    return _make_401(str(e), code="invalid_jwt")

        request.state.auth_context = ctx
        request.state.tenant_id = ctx.tenant_id   # backward compat for existing routes
        bind_context(tenant_id=ctx.tenant_id)
        return await call_next(request)
