"""Unit tests for domain/streaming/events.py.

Pure — no I/O, no Docker.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from trendstorm.domain.streaming.events import StreamEvent, StreamEventType
from trendstorm.shared.ids import new_id

JOB_ID = new_id()
TENANT_ID = new_id()


@pytest.mark.unit
class TestStreamEventType:
    def test_all_types_are_strings(self) -> None:
        for t in StreamEventType:
            assert isinstance(t, str)

    def test_terminal_types(self) -> None:
        assert StreamEventType.REPORT_READY.is_terminal
        assert StreamEventType.JOB_FAILED.is_terminal

    def test_non_terminal_types(self) -> None:
        for t in StreamEventType:
            if t not in {StreamEventType.REPORT_READY, StreamEventType.JOB_FAILED}:
                assert not t.is_terminal, f"{t} should not be terminal"

    def test_roundtrip_from_string(self) -> None:
        assert StreamEventType("stage_started") is StreamEventType.STAGE_STARTED
        assert StreamEventType("report_ready") is StreamEventType.REPORT_READY


@pytest.mark.unit
class TestStreamEvent:
    def _make(self, **kwargs) -> StreamEvent:
        return StreamEvent(
            job_id=JOB_ID,
            tenant_id=TENANT_ID,
            event_type=StreamEventType.STAGE_STARTED,
            **kwargs,
        )

    def test_default_seq_is_zero(self) -> None:
        ev = self._make()
        assert ev.seq == 0

    def test_default_event_id_is_ulid(self) -> None:
        ev = self._make()
        assert len(ev.event_id) == 26
        assert ev.event_id.isalnum()

    def test_event_ids_unique(self) -> None:
        a = self._make()
        b = self._make()
        assert a.event_id != b.event_id

    def test_with_seq_returns_new_instance(self) -> None:
        ev = self._make()
        stamped = ev.with_seq(42)
        assert stamped.seq == 42
        assert ev.seq == 0  # original unchanged (frozen)
        assert stamped.event_id == ev.event_id

    def test_stage_optional(self) -> None:
        ev = self._make()
        assert ev.stage is None
        with_stage = self._make(stage="ingesting")
        assert with_stage.stage == "ingesting"

    def test_payload_defaults_empty(self) -> None:
        ev = self._make()
        assert ev.payload == {}

    def test_payload_arbitrary_data(self) -> None:
        ev = self._make(payload={"count": 5, "pct": 0.5, "items": ["a", "b"]})
        assert ev.payload["count"] == 5
        assert ev.payload["items"] == ["a", "b"]

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            StreamEvent(
                job_id=JOB_ID,
                tenant_id=TENANT_ID,
                event_type=StreamEventType.PROGRESS,
                unknown_field="oops",
            )

    def test_frozen_model_immutable(self) -> None:
        ev = self._make()
        with pytest.raises(ValidationError):
            ev.seq = 99  # type: ignore[misc]

    def test_occurred_at_is_utc(self) -> None:
        ev = self._make()
        assert ev.occurred_at.tzinfo is not None
        assert ev.occurred_at.utcoffset().total_seconds() == 0  # type: ignore[union-attr]

    def test_serialization_round_trip(self) -> None:
        ev = self._make(
            stage="ingesting",
            payload={"sources": 3},
        ).with_seq(7)
        data = ev.model_dump(mode="json")
        restored = StreamEvent.model_validate(data)
        assert restored.seq == 7
        assert restored.event_type == StreamEventType.STAGE_STARTED
        assert restored.payload == {"sources": 3}
        assert restored.stage == "ingesting"

    def test_job_failed_is_terminal(self) -> None:
        ev = StreamEvent(
            job_id=JOB_ID,
            tenant_id=TENANT_ID,
            event_type=StreamEventType.JOB_FAILED,
        )
        assert ev.event_type.is_terminal

    def test_report_ready_is_terminal(self) -> None:
        ev = StreamEvent(
            job_id=JOB_ID,
            tenant_id=TENANT_ID,
            event_type=StreamEventType.REPORT_READY,
        )
        assert ev.event_type.is_terminal
