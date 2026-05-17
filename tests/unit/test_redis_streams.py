"""Unit tests for infrastructure/redis/streams.py.

Uses fakeredis for a real in-process Redis experience without Docker.
"""
from __future__ import annotations

import fakeredis.aioredis as fakeredis_async
import pytest

from trendstorm.infrastructure.redis.streams import RedisStreamStore
from trendstorm.shared.config import SSESettings
from trendstorm.shared.errors import DatabaseError


def _store() -> RedisStreamStore:
    return RedisStreamStore(SSESettings())


async def _connected_store() -> tuple[RedisStreamStore, fakeredis_async.FakeRedis]:
    r = fakeredis_async.FakeRedis(decode_responses=True)
    store = _store()
    store.init(r)
    return store, r


@pytest.mark.unit
class TestRedisStreamStoreWrite:
    async def test_append_returns_entry_id(self) -> None:
        store, _ = await _connected_store()
        entry_id = await store.append("job1", {"seq": 1, "event_type": "stage_started"})
        assert isinstance(entry_id, str)
        assert "-" in entry_id  # Redis Stream ID format: <ms>-<seq>

    async def test_append_sets_ttl(self) -> None:
        store, r = await _connected_store()
        await store.append("job1", {"seq": 1})
        key = await store.stream_key("job1")
        ttl = await r.ttl(key)
        # Default TTL = 24h = 86400s; allow some skew
        assert ttl > 86390

    async def test_incr_seq_starts_at_one(self) -> None:
        store, _ = await _connected_store()
        seq = await store.incr_seq("job1")
        assert seq == 1

    async def test_incr_seq_monotonic(self) -> None:
        store, _ = await _connected_store()
        seqs = [await store.incr_seq("job1") for _ in range(5)]
        assert seqs == [1, 2, 3, 4, 5]

    async def test_incr_seq_isolated_per_job(self) -> None:
        store, _ = await _connected_store()
        await store.incr_seq("job-a")
        await store.incr_seq("job-a")
        seq_b = await store.incr_seq("job-b")
        assert seq_b == 1

    async def test_append_raises_if_not_initialised(self) -> None:
        store = _store()
        with pytest.raises(DatabaseError):
            await store.append("job1", {"seq": 1})


@pytest.mark.unit
class TestRedisStreamStoreRead:
    async def test_read_from_zero_returns_all(self) -> None:
        store, _ = await _connected_store()
        for i in range(1, 4):
            await store.append("job1", {"seq": i, "event_type": "progress"})
        events = await store.read_from("job1", min_seq=0)
        assert len(events) == 3
        assert [e["seq"] for e in events] == [1, 2, 3]

    async def test_read_from_min_seq_filters(self) -> None:
        store, _ = await _connected_store()
        for i in range(1, 6):
            await store.append("job1", {"seq": i, "event_type": "progress"})
        events = await store.read_from("job1", min_seq=3)
        assert len(events) == 3
        assert events[0]["seq"] == 3

    async def test_read_from_empty_stream(self) -> None:
        store, _ = await _connected_store()
        events = await store.read_from("nonexistent_job")
        assert events == []

    async def test_read_from_high_min_seq_returns_empty(self) -> None:
        store, _ = await _connected_store()
        await store.append("job1", {"seq": 1})
        events = await store.read_from("job1", min_seq=100)
        assert events == []

    async def test_read_preserves_payload_fields(self) -> None:
        store, _ = await _connected_store()
        payload = {"seq": 1, "event_type": "stage_started", "stage": "ingesting", "nested": {"k": "v"}}
        await store.append("job1", payload)
        events = await store.read_from("job1")
        assert events[0]["event_type"] == "stage_started"
        assert events[0]["nested"] == {"k": "v"}

    async def test_jobs_are_isolated(self) -> None:
        store, _ = await _connected_store()
        await store.append("job-a", {"seq": 1, "data": "a"})
        await store.append("job-b", {"seq": 1, "data": "b"})
        events_a = await store.read_from("job-a")
        events_b = await store.read_from("job-b")
        assert len(events_a) == 1
        assert len(events_b) == 1
        assert events_a[0]["data"] == "a"
        assert events_b[0]["data"] == "b"

    async def test_stream_key_format(self) -> None:
        store = _store()
        key = await store.stream_key("abc123")
        assert key == "stream:abc123:events"

    async def test_read_raises_if_not_initialised(self) -> None:
        store = _store()
        with pytest.raises(DatabaseError):
            await store.read_from("job1")
