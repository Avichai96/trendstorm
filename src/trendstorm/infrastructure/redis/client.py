"""Redis async client wrapper.

Owns the redis-py asyncio client. One instance per process.

Use cases:
    - Semantic cache (Phase 7)
    - Idempotency keys for at-least-once Kafka consumers
    - SSE offset tracking
    - Rate limiting (future)

Note: redis-py's asyncio client uses a connection pool internally;
we don't need explicit pooling on top.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from redis.asyncio import Redis, from_url
from redis.exceptions import RedisError

from trendstorm.shared.config import RedisSettings
from trendstorm.shared.errors import DatabaseError
from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)


class RedisClient:
    """Async Redis client lifecycle manager."""

    def __init__(self, settings: RedisSettings) -> None:
        self._settings = settings
        self._client: Redis | None = None

    async def connect(self) -> None:
        """Create the client and verify with PING. Idempotent."""
        if self._client is not None:
            return

        logger.info("redis_connecting")
        self._client = from_url(  # type: ignore[no-untyped-call]  # redis.asyncio.from_url lacks complete stubs
            self._settings.url.get_secret_value(),
            max_connections=self._settings.max_connections,
            decode_responses=True,
            # health_check_interval pings idle connections; catches stale ones
            health_check_interval=30,
        )
        try:
            pong = await self._client.ping()
            if not pong:
                raise DatabaseError("Redis PING returned falsy", context={"pong": pong})
        except RedisError as e:
            await self._safe_close()
            raise DatabaseError(
                "Redis connection failed during startup",
                context={"error": str(e), "error_type": type(e).__name__},
            ) from e
        logger.info("redis_connected")

    async def close(self) -> None:
        """Close the connection pool. Idempotent."""
        await self._safe_close()

    async def _safe_close(self) -> None:
        if self._client is None:
            return
        logger.info("redis_closing")
        try:
            await self._client.aclose()
        except RedisError as e:
            logger.warning("redis_close_error", error=str(e))
        finally:
            self._client = None

    @property
    def client(self) -> Redis:
        """The underlying client. Raises if not connected."""
        if self._client is None:
            raise DatabaseError("Redis client not initialized; call connect() first")
        return self._client

    async def health_check(self) -> bool:
        """Fast non-throwing health check."""
        if self._client is None:
            return False
        try:
            return bool(await self._client.ping())
        except RedisError:
            return False
