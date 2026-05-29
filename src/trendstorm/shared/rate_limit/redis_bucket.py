"""Generic Redis token-bucket rate limiter.

Extracted from `agents/scout/rate_limit.py` (Phase 6) so both the Scout
ingestion worker and the API rate-limit middleware can share the same Lua
script without duplication.

The scout module keeps its own `RateLimiter` wrapper for backward
compatibility, but delegates the Lua script to this module.

Algorithm: token bucket with Redis atomic Lua script.
- Each key identifies a bucket (any string).
- Bucket refills at `rate` tokens/second up to `burst` capacity.
- Each acquire call consumes one token.
- If empty: returns (False, wait_ms) so the caller can 429/backoff.

The Lua script is atomic — no TOCTOU race between read and write.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis


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


class RedisBucket:
    """Generic token-bucket rate limiter backed by Redis.

    Args:
        redis:  An async Redis client instance.
        rate:   Tokens refilled per second.
        burst:  Maximum bucket capacity (also the initial fill).

    """

    def __init__(self, redis: Redis, *, rate: float, burst: int) -> None:
        self._rate = rate
        self._burst = burst
        self._script = redis.register_script(_SCRIPT)

    async def acquire(
        self,
        key: str,
        *,
        _now: float | None = None,
    ) -> tuple[bool, int]:
        """Try to consume one token for `key`.

        Returns:
            (allowed, wait_ms). wait_ms > 0 only when denied.

        """
        now = _now if _now is not None else time.time()
        result: list[int] = await self._script(
            keys=[key],
            args=[now, self._rate, self._burst, 1],
        )
        return bool(result[0]), int(result[1])
