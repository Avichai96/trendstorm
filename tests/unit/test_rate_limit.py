"""Unit tests for agents/scout/rate_limit.py.

Strategy:
  - Mock the Redis script call so tests run without infrastructure.
  - Inject a fake clock via the `_now` parameter to make timing deterministic.
  - Test the Python-layer contract: key structure, arg passing, result
    interpretation.
  - The Lua bucket math (refill, wait_ms calculation) is tested in
    tests/integration/test_rate_limit_redis.py against a real Redis instance.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from trendstorm.agents.scout.rate_limit import _KEY_PREFIX, RateLimiter


def _make_limiter(
    script_result: list[int],
    *,
    rate: float = 2.0,
    burst: int = 5,
) -> tuple[RateLimiter, AsyncMock]:
    """Return a RateLimiter wired to a mock Redis that returns `script_result`."""
    redis: Any = MagicMock()
    script = AsyncMock(return_value=script_result)
    redis.register_script.return_value = script
    limiter = RateLimiter(redis, rate=rate, burst=burst)
    return limiter, script


@pytest.mark.unit
class TestBucketKey:
    def test_prefix_tenant_host(self) -> None:
        redis: Any = MagicMock()
        redis.register_script.return_value = AsyncMock()
        rl = RateLimiter(redis, rate=2.0, burst=5)
        assert rl.bucket_key("t1", "example.com") == f"{_KEY_PREFIX}:t1:example.com"

    def test_different_tenants_different_keys(self) -> None:
        redis: Any = MagicMock()
        redis.register_script.return_value = AsyncMock()
        rl = RateLimiter(redis, rate=2.0, burst=5)
        k1 = rl.bucket_key("alice", "api.example.com")
        k2 = rl.bucket_key("bob", "api.example.com")
        assert k1 != k2

    def test_different_hosts_different_keys(self) -> None:
        redis: Any = MagicMock()
        redis.register_script.return_value = AsyncMock()
        rl = RateLimiter(redis, rate=2.0, burst=5)
        k1 = rl.bucket_key("t1", "foo.com")
        k2 = rl.bucket_key("t1", "bar.com")
        assert k1 != k2


@pytest.mark.unit
class TestAcquireReturnValues:
    async def test_allowed_when_script_returns_1_0(self) -> None:
        rl, _ = _make_limiter([1, 0])
        allowed, wait_ms = await rl.acquire("t1", "host.com", _now=1000.0)
        assert allowed is True
        assert wait_ms == 0

    async def test_denied_when_script_returns_0_wait(self) -> None:
        rl, _ = _make_limiter([0, 500])
        allowed, wait_ms = await rl.acquire("t1", "host.com", _now=1000.0)
        assert allowed is False
        assert wait_ms == 500

    async def test_wait_ms_is_int(self) -> None:
        rl, _ = _make_limiter([0, 123])
        _, wait_ms = await rl.acquire("t1", "host.com", _now=1000.0)
        assert isinstance(wait_ms, int)


@pytest.mark.unit
class TestScriptArgPassing:
    async def test_keys_contain_bucket_key(self) -> None:
        rl, script = _make_limiter([1, 0])
        await rl.acquire("tenantA", "api.example.com", _now=1000.0)
        call_keys = script.call_args.kwargs["keys"]
        assert call_keys == [f"{_KEY_PREFIX}:tenantA:api.example.com"]

    async def test_args_now_matches_injected_clock(self) -> None:
        rl, script = _make_limiter([1, 0])
        await rl.acquire("t1", "host.com", _now=42.5)
        args = script.call_args.kwargs["args"]
        assert args[0] == 42.5

    async def test_args_rate_matches_constructor(self) -> None:
        rl, script = _make_limiter([1, 0], rate=3.5)
        await rl.acquire("t1", "host.com", _now=0.0)
        args = script.call_args.kwargs["args"]
        assert args[1] == 3.5

    async def test_args_burst_matches_constructor(self) -> None:
        rl, script = _make_limiter([1, 0], burst=10)
        await rl.acquire("t1", "host.com", _now=0.0)
        args = script.call_args.kwargs["args"]
        assert args[2] == 10

    async def test_cost_is_always_1(self) -> None:
        rl, script = _make_limiter([1, 0])
        await rl.acquire("t1", "host.com", _now=0.0)
        args = script.call_args.kwargs["args"]
        assert args[3] == 1


@pytest.mark.unit
class TestFakeClockMath:
    """Verify that the Python side correctly threads the fake clock through.

    These tests simulate what the Lua script *would* return given known bucket
    state, and assert that acquire() faithfully reports those results. The Lua
    math itself is validated in integration tests.
    """

    async def test_full_bucket_allows_request(self) -> None:
        # Simulate: bucket is full (5 tokens), consume 1 → allowed
        rl, _ = _make_limiter([1, 0], burst=5)
        allowed, _ = await rl.acquire("t", "h", _now=0.0)
        assert allowed is True

    async def test_empty_bucket_denies_request(self) -> None:
        # Simulate: bucket empty at rate=2, cost=1 → wait = ceil(1/2*1000) = 500ms
        rl, _ = _make_limiter([0, 500])
        allowed, wait_ms = await rl.acquire("t", "h", _now=0.0)
        assert allowed is False
        assert wait_ms == 500

    async def test_partial_bucket_gives_correct_wait(self) -> None:
        # Simulate: 0.3 tokens left, rate=1 → wait = ceil((1-0.3)/1*1000) = 700ms
        rl, _ = _make_limiter([0, 700])
        _, wait_ms = await rl.acquire("t", "h", _now=0.0)
        assert wait_ms == 700
