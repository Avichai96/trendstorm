"""Per-host Redis token-bucket rate limiter for the Scout ingestion worker.

One RateLimiter instance is shared across all concurrent fetch tasks in a
worker process. Bucket state lives in Redis so multiple worker replicas share
the same limit for a given (tenant_id, host) pair.

Algorithm: token bucket.
    - Each (tenant_id, host) pair gets its own bucket.
    - Bucket refills at `rate` tokens/second up to `burst` capacity.
    - Each request consumes one token.
    - If the bucket is empty the request is denied and wait_ms is returned.

The Lua script executes atomically — no TOCTOU race between read and write.
Redis compiles it once per connection (EVALSHA after the first EVAL).

Key format:  scout:rl:{tenant_id}:{host}
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis


# ---------------------------------------------------------------------------
# Lua token-bucket script
#
# KEYS[1]: bucket key
# ARGV[1]: now (float seconds since epoch)
# ARGV[2]: rate (tokens to refill per second)
# ARGV[3]: capacity (burst limit / max tokens)
# ARGV[4]: cost (tokens to consume; always 1)
#
# Returns {1, 0} → allowed
#         {0, wait_ms} → denied; wait_ms = min ms before next token
# ---------------------------------------------------------------------------
_SCRIPT = """\
local key      = KEYS[1]
local now      = tonumber(ARGV[1])
local rate     = tonumber(ARGV[2])
local capacity = tonumber(ARGV[3])
local cost     = tonumber(ARGV[4])
local data  = redis.call('HMGET', key, 't', 'ts')
local tokens = tonumber(data[1]) or capacity
local last   = tonumber(data[2]) or now
local elapsed = math.max(0, now - last)
local filled  = math.min(capacity, tokens + elapsed * rate)
if filled >= cost then
    redis.call('HMSET', key, 't', filled - cost, 'ts', now)
    redis.call('EXPIRE', key, 3600)
    return {1, 0}
else
    local wait = math.ceil((cost - filled) / rate * 1000)
    redis.call('HMSET', key, 't', filled, 'ts', now)
    redis.call('EXPIRE', key, 3600)
    return {0, wait}
end
"""

_KEY_PREFIX = "scout:rl"


class RateLimiter:
    """Per-(tenant_id, host) token-bucket rate limiter backed by Redis."""

    def __init__(
        self,
        redis: Redis,
        *,
        rate: float = 2.0,
        burst: int = 5,
    ) -> None:
        self._rate = rate
        self._burst = burst
        self._script = redis.register_script(_SCRIPT)

    # ------------------------------------------------------------------

    def bucket_key(self, tenant_id: str, host: str) -> str:
        """Canonical Redis key for a (tenant_id, host) bucket."""
        return f"{_KEY_PREFIX}:{tenant_id}:{host}"

    async def acquire(
        self,
        tenant_id: str,
        host: str,
        *,
        _now: float | None = None,
    ) -> tuple[bool, int]:
        """Try to consume one token for (tenant_id, host).

        Args:
            tenant_id: Tenant scope — buckets are isolated per tenant.
            host: Hostname extracted from the URL being fetched.
            _now: Override current time (seconds). Used by unit tests only.

        Returns:
            (allowed, wait_ms).  wait_ms > 0 only when denied.

        """
        now = _now if _now is not None else time.time()
        result: list[int] = await self._script(
            keys=[self.bucket_key(tenant_id, host)],
            args=[now, self._rate, self._burst, 1],
        )
        return bool(result[0]), int(result[1])
