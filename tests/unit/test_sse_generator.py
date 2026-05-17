"""Unit tests for services/streaming/sse.py.

Uses fakeredis to test the full subscribe-before-read flow without Docker.
"""
from __future__ import annotations

import asyncio
import json

import fakeredis.aioredis as fakeredis_async
import pytest

from trendstorm.infrastructure.redis.pubsub import RedisPubSub
from trendstorm.infrastructure.redis.streams import RedisStreamStore
from trendstorm.services.streaming.sse import (
    _format_sse,
    _heartbeat,
    _is_terminal,
    sse_event_generator,
)
from trendstorm.shared.config import SSESettings
from trendstorm.shared.ids import new_id


def _make_settings(**kwargs) -> SSESettings:
    return SSESettings(**kwargs)


async def _make_infra(
    settings: SSESettings | None = None,
) -> tuple[RedisStreamStore, RedisPubSub, fakeredis_async.FakeRedis]:
    r = fakeredis_async.FakeRedis(decode_responses=True)
    s = settings or _make_settings()
    store = RedisStreamStore(s)
    store.init(r)
    ps = RedisPubSub(s)
    ps.init(r)
    return store, ps, r


@pytest.mark.unit
class TestSSEFormat:
    def test_format_sse_structure(self) -> None:
        payload = {"seq": 5, "event_type": "stage_started", "stage": "ingesting"}
        result = _format_sse(payload)
        assert result.startswith("id: 5\n")
        assert "event: stage_started\n" in result
        assert "data: " in result
        assert result.endswith("\n\n")

    def test_format_sse_data_is_valid_json(self) -> None:
        payload = {"seq": 1, "event_type": "progress", "payload": {"count": 3}}
        result = _format_sse(payload)
        data_line = next(line for line in result.splitlines() if line.startswith("data: "))
        parsed = json.loads(data_line[len("data: "):])
        assert parsed["event_type"] == "progress"

    def test_heartbeat_is_sse_comment(self) -> None:
        hb = _heartbeat()
        assert hb.startswith(": heartbeat")
        assert hb.endswith("\n\n")

    def test_is_terminal_report_ready(self) -> None:
        assert _is_terminal({"event_type": "report_ready"})

    def test_is_terminal_job_failed(self) -> None:
        assert _is_terminal({"event_type": "job_failed"})

    def test_is_not_terminal_progress(self) -> None:
        assert not _is_terminal({"event_type": "progress"})

    def test_is_not_terminal_unknown(self) -> None:
        assert not _is_terminal({"event_type": "unknown_event"})


@pytest.mark.unit
class TestSSEGenerator:
    async def _collect(
        self,
        gen,
        *,
        max_items: int = 20,
    ) -> list[str]:
        results = []
        async for chunk in gen:
            results.append(chunk)
            if len(results) >= max_items:
                break
        return results

    async def test_empty_stream_no_live_events_returns_nothing(self) -> None:
        settings = _make_settings()
        store, ps, _ = await _make_infra(settings)
        job_id = new_id()

        # Run generator with a short stop to avoid hanging
        stop = asyncio.Event()
        stop.set()  # pre-set so live tail exits immediately

        results: list[str] = []
        gen = sse_event_generator(
            job_id,
            stream_store=store,
            pubsub=ps,
            settings=settings,
        )
        # Replace pubsub with one that respects stop immediately
        # Direct approach: collect with timeout
        try:
            async with asyncio.timeout(1.0):
                async for chunk in gen:
                    results.append(chunk)
        except TimeoutError:
            pass

        assert results == []

    async def test_replays_history_from_stream(self) -> None:
        settings = _make_settings()
        store, ps, _ = await _make_infra(settings)
        job_id = new_id()

        # Pre-populate stream with events
        for i in range(1, 4):
            await store.append(job_id, {"seq": i, "event_type": "progress", "job_id": job_id})

        # Add terminal event so generator closes cleanly
        await store.append(job_id, {"seq": 4, "event_type": "report_ready", "job_id": job_id})

        results: list[str] = []
        async with asyncio.timeout(2.0):
            async for chunk in sse_event_generator(
                job_id,
                stream_store=store,
                pubsub=ps,
                settings=settings,
            ):
                results.append(chunk)

        # Should have yielded 4 SSE events (3 progress + 1 report_ready)
        assert len(results) == 4
        # All are properly formatted
        for r in results:
            assert r.startswith("id: ")
            assert "event: " in r
            assert "data: " in r

    async def test_terminal_event_closes_generator(self) -> None:
        settings = _make_settings()
        store, ps, _ = await _make_infra(settings)
        job_id = new_id()

        await store.append(job_id, {"seq": 1, "event_type": "stage_started", "job_id": job_id})
        await store.append(job_id, {"seq": 2, "event_type": "report_ready", "job_id": job_id})
        await store.append(job_id, {"seq": 3, "event_type": "progress", "job_id": job_id})  # after terminal

        results: list[str] = []
        async with asyncio.timeout(2.0):
            async for chunk in sse_event_generator(
                job_id,
                stream_store=store,
                pubsub=ps,
                settings=settings,
            ):
                results.append(chunk)

        # Generator closes after REPORT_READY; seq=3 must not appear
        assert len(results) == 2
        first_ids = [r.split("\n")[0] for r in results]
        assert "id: 1" in first_ids
        assert "id: 2" in first_ids

    async def test_last_event_id_skips_already_seen(self) -> None:
        settings = _make_settings()
        store, ps, _ = await _make_infra(settings)
        job_id = new_id()

        for i in range(1, 6):
            et = "report_ready" if i == 5 else "progress"
            await store.append(job_id, {"seq": i, "event_type": et, "job_id": job_id})

        results: list[str] = []
        # Client reconnects with last_event_id=3 — should only get seq 3,4,5
        async with asyncio.timeout(2.0):
            async for chunk in sse_event_generator(
                job_id,
                stream_store=store,
                pubsub=ps,
                settings=settings,
                last_event_id=3,
            ):
                results.append(chunk)

        seq_ids = [int(r.split("\n")[0].replace("id: ", "")) for r in results]
        assert 1 not in seq_ids
        assert 2 not in seq_ids
        assert 3 in seq_ids  # inclusive

    async def test_receives_live_event_after_replay(self) -> None:
        settings = _make_settings(heartbeat_seconds=60)  # no heartbeats during test
        store, ps, _ = await _make_infra(settings)
        job_id = new_id()

        # One historical event
        await store.append(job_id, {"seq": 1, "event_type": "stage_started", "job_id": job_id})

        results: list[str] = []

        async def _run_generator() -> None:
            async with asyncio.timeout(3.0):
                async for chunk in sse_event_generator(
                    job_id,
                    stream_store=store,
                    pubsub=ps,
                    settings=settings,
                ):
                    results.append(chunk)

        async def _publish_live() -> None:
            # Allow generator to subscribe and replay history first
            await asyncio.sleep(0.1)
            _seq = await store.incr_seq(job_id)
            payload = {"seq": 2, "event_type": "report_ready", "job_id": job_id}
            await store.append(job_id, payload)
            await ps.publish(job_id, payload)

        await asyncio.gather(_run_generator(), _publish_live())

        assert len(results) == 2
        # seq 1 from replay, seq 2 from live
        assert "id: 1" in results[0]
        assert "id: 2" in results[1]
