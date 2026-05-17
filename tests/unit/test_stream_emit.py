"""Unit tests for services/streaming/emit.py.

Uses a fake producer to verify Kafka message construction without Docker.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from trendstorm.domain.streaming.events import StreamEvent, StreamEventType
from trendstorm.orchestration.topics import Topic
from trendstorm.services.streaming.emit import emit_stream_event
from trendstorm.shared.ids import new_id


def _make_event(**kwargs) -> StreamEvent:
    return StreamEvent(
        job_id=new_id(),
        tenant_id=new_id(),
        event_type=kwargs.pop("event_type", StreamEventType.STAGE_STARTED),
        **kwargs,
    )


def _make_producer() -> tuple[Any, list[dict]]:
    """Return (fake_producer, captured_calls)."""
    captured: list[dict] = []

    async def _fake_send_and_wait(topic: str, *, key: bytes, value: bytes) -> None:
        captured.append({"topic": topic, "key": key.decode(), "value": json.loads(value)})

    fake_aiokafka = MagicMock()
    fake_aiokafka.send_and_wait = AsyncMock(side_effect=_fake_send_and_wait)

    fake_producer = MagicMock()
    fake_producer.producer = fake_aiokafka

    return fake_producer, captured


@pytest.mark.unit
class TestEmitStreamEvent:
    async def test_publishes_to_correct_topic(self) -> None:
        event = _make_event()
        producer, calls = _make_producer()
        await emit_stream_event(event, producer=producer, correlation_id=new_id())
        assert len(calls) == 1
        assert calls[0]["topic"] == Topic.STREAM_PARTIAL.value

    async def test_key_is_job_id(self) -> None:
        event = _make_event()
        producer, calls = _make_producer()
        await emit_stream_event(event, producer=producer, correlation_id=new_id())
        assert calls[0]["key"] == event.job_id

    async def test_value_contains_stream_event_type(self) -> None:
        event = _make_event(event_type=StreamEventType.PROGRESS)
        producer, calls = _make_producer()
        await emit_stream_event(event, producer=producer, correlation_id=new_id())
        msg = calls[0]["value"]
        assert msg["stream_event_type"] == "progress"

    async def test_value_contains_stage(self) -> None:
        event = _make_event(stage="ingesting")
        producer, calls = _make_producer()
        await emit_stream_event(event, producer=producer, correlation_id=new_id())
        assert calls[0]["value"]["stage"] == "ingesting"

    async def test_value_contains_payload(self) -> None:
        event = _make_event(payload={"count": 5})
        producer, calls = _make_producer()
        await emit_stream_event(event, producer=producer, correlation_id=new_id())
        assert calls[0]["value"]["stream_payload"] == {"count": 5}

    async def test_value_carries_event_id_for_dedup(self) -> None:
        event = _make_event()
        producer, calls = _make_producer()
        await emit_stream_event(event, producer=producer, correlation_id=new_id())
        assert calls[0]["value"]["event_id"] == event.event_id

    async def test_value_carries_tenant_id(self) -> None:
        event = _make_event()
        producer, calls = _make_producer()
        await emit_stream_event(event, producer=producer, correlation_id=new_id())
        assert calls[0]["value"]["tenant_id"] == event.tenant_id

    async def test_producer_error_is_swallowed(self) -> None:
        """Emit must never propagate errors — it's best-effort UX."""
        event = _make_event()

        fake_aiokafka = MagicMock()
        fake_aiokafka.send_and_wait = AsyncMock(side_effect=RuntimeError("kafka down"))
        fake_producer = MagicMock()
        fake_producer.producer = fake_aiokafka

        # Must NOT raise
        await emit_stream_event(event, producer=fake_producer, correlation_id=new_id())

    async def test_no_duplicate_messages_for_same_event(self) -> None:
        """Each call to emit_stream_event sends exactly one message."""
        event = _make_event()
        producer, calls = _make_producer()
        await emit_stream_event(event, producer=producer, correlation_id=new_id())
        assert len(calls) == 1
