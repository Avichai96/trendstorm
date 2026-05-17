"""Unit tests for infrastructure/redis/pubsub.py.

Uses fakeredis for in-process pub/sub without Docker.
"""
from __future__ import annotations

import asyncio

import fakeredis.aioredis as fakeredis_async
import pytest

from trendstorm.infrastructure.redis.pubsub import RedisPubSub
from trendstorm.shared.config import SSESettings
from trendstorm.shared.errors import DatabaseError


def _pubsub() -> RedisPubSub:
    return RedisPubSub(SSESettings())


async def _connected() -> tuple[RedisPubSub, fakeredis_async.FakeRedis]:
    r = fakeredis_async.FakeRedis(decode_responses=True)
    ps = _pubsub()
    ps.init(r)
    return ps, r


@pytest.mark.unit
class TestRedisPubSubChannel:
    def test_channel_name_format(self) -> None:
        ps = _pubsub()
        assert ps.channel("abc") == "stream:abc:live"

    def test_channel_prefix_used(self) -> None:
        ps = RedisPubSub(SSESettings(channel_prefix="ts"))
        assert ps.channel("job1") == "ts:job1:live"


@pytest.mark.unit
class TestRedisPubSubPublish:
    async def test_publish_returns_receiver_count(self) -> None:
        ps, _ = await _connected()
        # No subscribers yet — should return 0 (not an error)
        count = await ps.publish("job1", {"seq": 1})
        assert count == 0

    async def test_publish_raises_if_not_initialised(self) -> None:
        ps = _pubsub()
        with pytest.raises(DatabaseError):
            await ps.publish("job1", {"seq": 1})


@pytest.mark.unit
class TestRedisPubSubSubscribe:
    async def test_subscribe_raises_if_not_initialised(self) -> None:
        ps = _pubsub()
        with pytest.raises(DatabaseError):
            await ps.subscribe("job1")

    async def test_receive_published_messages(self) -> None:
        ps, _ = await _connected()
        received: list[dict] = []

        async def _consumer() -> None:
            it = await ps.subscribe("job1")
            async for payload in it:
                received.append(payload)
                if len(received) >= 2:
                    await it.aclose()
                    break

        async def _producer() -> None:
            # Small delay to ensure subscriber is ready
            await asyncio.sleep(0.05)
            await ps.publish("job1", {"seq": 1, "event_type": "progress"})
            await ps.publish("job1", {"seq": 2, "event_type": "stage_completed"})

        await asyncio.gather(_consumer(), _producer())
        assert len(received) == 2
        assert received[0]["event_type"] == "progress"
        assert received[1]["event_type"] == "stage_completed"

    async def test_stop_event_halts_iteration(self) -> None:
        ps, _ = await _connected()
        stop = asyncio.Event()
        received: list[dict] = []

        async def _consumer() -> None:
            it = await ps.subscribe("job1", stop_event=stop)
            async for payload in it:
                received.append(payload)

        async def _producer() -> None:
            await asyncio.sleep(0.05)
            await ps.publish("job1", {"seq": 1})
            await asyncio.sleep(0.05)
            stop.set()

        await asyncio.gather(_consumer(), _producer())
        assert len(received) == 1

    async def test_channels_are_isolated(self) -> None:
        ps, _ = await _connected()
        received_a: list[dict] = []
        received_b: list[dict] = []

        async def _consumer_a() -> None:
            it = await ps.subscribe("job-a")
            async for payload in it:
                received_a.append(payload)
                await it.aclose()
                break

        async def _consumer_b() -> None:
            it = await ps.subscribe("job-b")
            async for payload in it:
                received_b.append(payload)
                await it.aclose()
                break

        async def _producer() -> None:
            await asyncio.sleep(0.05)
            await ps.publish("job-a", {"job": "a"})
            await ps.publish("job-b", {"job": "b"})

        await asyncio.gather(_consumer_a(), _consumer_b(), _producer())
        assert len(received_a) == 1
        assert received_a[0]["job"] == "a"
        assert len(received_b) == 1
        assert received_b[0]["job"] == "b"
