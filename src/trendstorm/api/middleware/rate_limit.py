"""Per-tenant HTTP rate limiting middleware.

Uses the shared Redis token-bucket (`shared/rate_limit/redis_bucket.py`) to
enforce a per-tenant request limit. The bucket key is `api:rl:{tenant_id}`.

Defaults (configurable via AuthSettings):
  rate_limit_requests_per_minute: 100
  rate_limit_burst: 20

A denied request receives 429 with `Retry-After: <seconds>` and
`X-RateLimit-Limit` / `X-RateLimit-Remaining` headers (partial; Remaining is
not computed precisely to avoid a second Redis round-trip).

Public paths and unauthenticated requests bypass rate limiting — the auth
middleware 401s unauthenticated requests before we reach this middleware.

Middleware ordering in `api/main.py` (outer → inner):
  CorrelationId → AuthMiddleware → RateLimitMiddleware → TenantMiddleware → CORS
This ensures `request.state.auth_context` is set before rate limiting.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse

from trendstorm.shared.logging import get_logger
from trendstorm.shared.rate_limit.redis_bucket import RedisBucket

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp

    from trendstorm.shared.config import AuthSettings

logger = get_logger(__name__)

_PUBLIC_PATHS = frozenset({
    "/health/live", "/health/ready",
    "/docs", "/openapi.json", "/redoc",
    "/metrics",
})

_KEY_PREFIX = "api:rl"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Token-bucket rate limiting per authenticated tenant.

    The Redis bucket is initialized lazily from request.app.state.redis on the
    first request — the Redis client is not connected at create_app() time.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        settings: AuthSettings,
    ) -> None:
        super().__init__(app)
        self._rate_per_second = settings.rate_limit_requests_per_minute / 60.0
        self._burst = settings.rate_limit_burst
        self._limit = settings.rate_limit_requests_per_minute
        self._bucket: RedisBucket | None = None

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        auth_ctx = getattr(request.state, "auth_context", None)
        if auth_ctx is None:
            # No auth context — auth middleware didn't run or public path.
            return await call_next(request)

        # Lazy-init the bucket from the lifespan-connected Redis client.
        if self._bucket is None:
            redis_client = getattr(request.app.state, "redis", None)
            if redis_client is not None:
                self._bucket = RedisBucket(
                    redis_client.client,
                    rate=self._rate_per_second,
                    burst=self._burst,
                )
        if self._bucket is None:
            # Redis not yet connected (startup race) — skip rate limiting.
            return await call_next(request)

        key = f"{_KEY_PREFIX}:{auth_ctx.tenant_id}"
        allowed, wait_ms = await self._bucket.acquire(key)

        if not allowed:
            retry_after = max(1, wait_ms // 1000)
            logger.warning(
                "rate_limit.exceeded",
                tenant_id=auth_ctx.tenant_id,
                wait_ms=wait_ms,
            )
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "rate_limit_exceeded",
                        "message": f"Too many requests. Retry after {retry_after}s.",
                    }
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(self._limit),
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(self._limit)
        return response
