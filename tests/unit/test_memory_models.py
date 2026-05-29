"""Unit tests for memory domain models, events, and config — Phase 15.5."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from trendstorm.domain.memories.models import Memory, MemoryKind, MemorySource
from trendstorm.orchestration.events import (
    AnyEvent,
    MemoryCompletedEvent,
    MemoryPendingEvent,
)
from trendstorm.shared.config import MemorySettings, Settings
from trendstorm.shared.ids import new_id


def _now() -> datetime:
    return datetime.now(UTC)


def _memory(**overrides) -> Memory:
    defaults = dict(
        id=new_id(),
        tenant_id="tenant-a",
        category_id="cat-1",
        kind=MemoryKind.SEMANTIC,
        source=MemorySource.EXTRACTED,
        content="LLMs are increasingly used in agentic settings.",
        confidence=0.85,
        source_job_id=new_id(),
        source_analysis_id=new_id(),
        is_active=True,
        tags=["llm", "agents"],
        created_at=_now(),
        updated_at=_now(),
    )
    defaults.update(overrides)
    return Memory(**defaults)


@pytest.mark.unit
class TestMemoryModel:
    def test_roundtrip_json(self) -> None:
        m = _memory()
        restored = Memory.model_validate_json(m.model_dump_json())
        assert restored.id == m.id
        assert restored.kind == MemoryKind.SEMANTIC
        assert restored.confidence == 0.85

    def test_episodic_kind(self) -> None:
        m = _memory(kind=MemoryKind.EPISODIC, source=MemorySource.JOB_OUTCOME)
        assert m.kind == MemoryKind.EPISODIC
        assert m.source == MemorySource.JOB_OUTCOME

    def test_user_curated_source(self) -> None:
        m = _memory(source=MemorySource.USER_CURATED, confidence=1.0)
        assert m.source == MemorySource.USER_CURATED
        assert m.confidence == 1.0

    def test_confidence_bounds(self) -> None:
        with pytest.raises(ValidationError):
            _memory(confidence=1.01)
        with pytest.raises(ValidationError):
            _memory(confidence=-0.01)

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            Memory.model_validate(
                {
                    **_memory().model_dump(),
                    "extra_field": "should fail",
                }
            )

    def test_superseded_by_nullable(self) -> None:
        m = _memory(superseded_by=None)
        assert m.superseded_by is None
        m2 = _memory(superseded_by=new_id())
        assert m2.superseded_by is not None

    def test_embedding_id_nullable(self) -> None:
        m = _memory(content_embedding_id=None)
        assert m.content_embedding_id is None
        m2 = _memory(content_embedding_id="emb-123")
        assert m2.content_embedding_id == "emb-123"

    def test_tags_default_empty(self) -> None:
        m = _memory(tags=[])
        assert m.tags == []


@pytest.mark.unit
class TestMemoryPendingEvent:
    def _event(self, **overrides) -> MemoryPendingEvent:
        defaults = dict(
            event_id=new_id(),
            correlation_id="cid-1",
            tenant_id="tenant-a",
            job_id=new_id(),
            analysis_id=new_id(),
            category_id="cat-1",
        )
        defaults.update(overrides)
        return MemoryPendingEvent(**defaults)

    def test_default_attempt_is_one(self) -> None:
        e = self._event()
        assert e.attempt == 1

    def test_roundtrip_json(self) -> None:
        e = self._event(attempt=2)
        restored = MemoryPendingEvent.model_validate_json(e.model_dump_json())
        assert restored.attempt == 2
        assert restored.event_type == "memory.pending"

    def test_discriminated_union(self) -> None:
        e = self._event()
        adapter: TypeAdapter[AnyEvent] = TypeAdapter(AnyEvent)
        parsed = adapter.validate_json(e.model_dump_json())
        assert isinstance(parsed, MemoryPendingEvent)


@pytest.mark.unit
class TestMemoryCompletedEvent:
    def _event(self, **overrides) -> MemoryCompletedEvent:
        defaults = dict(
            event_id=new_id(),
            correlation_id="cid-1",
            tenant_id="tenant-a",
            job_id=new_id(),
            success=True,
        )
        defaults.update(overrides)
        return MemoryCompletedEvent(**defaults)

    def test_success_with_ids(self) -> None:
        e = self._event(
            episodic_memory_id="ep-1",
            semantic_memory_ids=["s1", "s2"],
        )
        assert e.episodic_memory_id == "ep-1"
        assert e.semantic_memory_ids == ["s1", "s2"]

    def test_partial_success_no_episodic(self) -> None:
        e = self._event(episodic_memory_id=None, semantic_memory_ids=["s1"])
        assert e.episodic_memory_id is None
        assert e.success is True

    def test_failure_event(self) -> None:
        e = self._event(
            success=False,
            error_code="extraction_failed",
            error_message="LLM returned no tool call",
        )
        assert e.success is False
        assert e.error_code == "extraction_failed"

    def test_discriminated_union(self) -> None:
        e = self._event()
        adapter: TypeAdapter[AnyEvent] = TypeAdapter(AnyEvent)
        parsed = adapter.validate_json(e.model_dump_json())
        assert isinstance(parsed, MemoryCompletedEvent)


@pytest.mark.unit
class TestMemorySettings:
    def test_defaults(self) -> None:
        s = MemorySettings()
        assert s.episodic_ttl_days == 730
        assert s.semantic_ttl_days == 730
        assert s.supersede_similarity_threshold == 0.92
        assert s.max_semantic_memories_per_job == 10
        assert s.memory_final_k == 5

    def test_settings_has_memory_field(self) -> None:
        s = Settings()
        assert hasattr(s, "memory")
        assert isinstance(s.memory, MemorySettings)

    def test_threshold_bounds(self) -> None:
        s = MemorySettings(supersede_similarity_threshold=0.99)
        assert s.supersede_similarity_threshold == 0.99
        with pytest.raises(ValidationError):
            MemorySettings(supersede_similarity_threshold=1.01)

    def test_max_memories_min(self) -> None:
        s = MemorySettings(max_semantic_memories_per_job=1)
        assert s.max_semantic_memories_per_job == 1
        with pytest.raises(ValidationError):
            MemorySettings(max_semantic_memories_per_job=0)
