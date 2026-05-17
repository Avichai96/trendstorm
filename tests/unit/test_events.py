"""Unit tests for orchestration/events.py — Pydantic serialization roundtrips."""
from __future__ import annotations

import pytest
from pydantic import TypeAdapter

from trendstorm.orchestration.events import (
    AnalysisCompletedEvent,
    AnalysisPendingEvent,
    AnyEvent,
    IngestCompletedEvent,
    IngestDocRef,
    IngestPendingEvent,
)
from trendstorm.shared.ids import new_id


def _base(event_type: str = "job.requested") -> dict:
    return {
        "event_type": event_type,
        "correlation_id": "cid-1",
        "tenant_id": "t1",
    }


@pytest.mark.unit
class TestIngestPendingEvent:
    def test_source_ids_is_list(self) -> None:
        e = IngestPendingEvent(
            **_base("ingest.pending"),
            job_id=new_id(),
            source_ids=["s1", "s2", "s3"],
        )
        assert e.source_ids == ["s1", "s2", "s3"]

    def test_roundtrip_json(self) -> None:
        e = IngestPendingEvent(
            **_base("ingest.pending"),
            job_id=new_id(),
            source_ids=["s1"],
            attempt=2,
        )
        restored = IngestPendingEvent.model_validate_json(e.model_dump_json())
        assert restored.source_ids == ["s1"]
        assert restored.attempt == 2

    def test_discriminated_union_dispatch(self) -> None:
        e = IngestPendingEvent(
            **_base("ingest.pending"),
            job_id=new_id(),
            source_ids=["s1"],
        )
        adapter: TypeAdapter[AnyEvent] = TypeAdapter(AnyEvent)
        parsed = adapter.validate_json(e.model_dump_json())
        assert isinstance(parsed, IngestPendingEvent)
        assert parsed.source_ids == ["s1"]

    def test_empty_source_ids_allowed(self) -> None:
        e = IngestPendingEvent(
            **_base("ingest.pending"),
            job_id=new_id(),
            source_ids=[],
        )
        assert e.source_ids == []

    def test_no_source_id_singular_field(self) -> None:
        e = IngestPendingEvent(
            **_base("ingest.pending"),
            job_id=new_id(),
            source_ids=["s1"],
        )
        assert not hasattr(e, "source_id")


@pytest.mark.unit
class TestIngestCompletedEvent:
    def test_document_refs_and_failed_source_ids(self) -> None:
        ref = IngestDocRef(
            id=new_id(), source_id="s1",
            content_hash="abc123", blob_uri_raw="s3://b/k", char_count=500,
        )
        e = IngestCompletedEvent(
            **_base("ingest.completed"),
            job_id=new_id(),
            document_refs=[ref],
            failed_source_ids=["s2"],
        )
        assert len(e.document_refs) == 1
        assert e.document_refs[0].id == ref.id
        assert e.failed_source_ids == ["s2"]

    def test_roundtrip_json(self) -> None:
        ref = IngestDocRef(id=new_id(), source_id="s1", content_hash="h1")
        e = IngestCompletedEvent(
            **_base("ingest.completed"),
            job_id=new_id(),
            document_refs=[ref],
            failed_source_ids=["s3"],
        )
        restored = IngestCompletedEvent.model_validate_json(e.model_dump_json())
        assert restored.document_refs[0].source_id == "s1"
        assert restored.failed_source_ids == ["s3"]

    def test_discriminated_union_dispatch(self) -> None:
        e = IngestCompletedEvent(
            **_base("ingest.completed"),
            job_id=new_id(),
            document_refs=[],
            failed_source_ids=["s1"],
        )
        adapter: TypeAdapter[AnyEvent] = TypeAdapter(AnyEvent)
        parsed = adapter.validate_json(e.model_dump_json())
        assert isinstance(parsed, IngestCompletedEvent)
        assert parsed.failed_source_ids == ["s1"]

    def test_partial_success_no_error_code(self) -> None:
        e = IngestCompletedEvent(
            **_base("ingest.completed"),
            job_id=new_id(),
            document_refs=[IngestDocRef(id=new_id(), source_id="s1", content_hash="h")],
            failed_source_ids=["s2"],
        )
        assert e.error_code is None

    def test_catastrophic_failure_sets_error_code(self) -> None:
        e = IngestCompletedEvent(
            **_base("ingest.completed"),
            job_id=new_id(),
            error_code="fetch_error",
            error_message="All sources failed",
        )
        assert e.document_refs == []
        assert e.error_code == "fetch_error"

    def test_no_legacy_success_bool_field(self) -> None:
        e = IngestCompletedEvent(
            **_base("ingest.completed"),
            job_id=new_id(),
        )
        assert not hasattr(e, "success")
        assert not hasattr(e, "source_id")
        assert not hasattr(e, "document_ids")


@pytest.mark.unit
class TestAnalysisPendingEvent:
    def test_required_fields(self) -> None:
        e = AnalysisPendingEvent(
            **_base("analysis.pending"),
            job_id=new_id(),
            category_id="cat-123",
        )
        assert e.category_id == "cat-123"
        assert e.refinement_loop == 0          # default
        assert e.refinement_notes is None       # default
        assert e.attempt == 1                   # default

    def test_refinement_fields_set(self) -> None:
        e = AnalysisPendingEvent(
            **_base("analysis.pending"),
            job_id=new_id(),
            category_id="cat-123",
            refinement_loop=2,
            refinement_notes="missing coverage of topic X",
        )
        assert e.refinement_loop == 2
        assert e.refinement_notes == "missing coverage of topic X"

    def test_roundtrip_json(self) -> None:
        e = AnalysisPendingEvent(
            **_base("analysis.pending"),
            job_id=new_id(),
            category_id="cat",
            refinement_loop=1,
            refinement_notes="please address ungrounded claim about X",
        )
        restored = AnalysisPendingEvent.model_validate_json(e.model_dump_json())
        assert restored.category_id == "cat"
        assert restored.refinement_loop == 1
        assert restored.refinement_notes == "please address ungrounded claim about X"

    def test_discriminated_union_dispatch(self) -> None:
        e = AnalysisPendingEvent(
            **_base("analysis.pending"),
            job_id=new_id(),
            category_id="cat",
        )
        adapter: TypeAdapter[AnyEvent] = TypeAdapter(AnyEvent)
        parsed = adapter.validate_json(e.model_dump_json())
        assert isinstance(parsed, AnalysisPendingEvent)
        assert parsed.category_id == "cat"

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValueError):
            AnalysisPendingEvent(  # type: ignore[call-arg]
                **_base("analysis.pending"),
                job_id=new_id(),
                category_id="cat",
                unknown_field="x",
            )


@pytest.mark.unit
class TestAnalysisCompletedEvent:
    def test_success_case(self) -> None:
        e = AnalysisCompletedEvent(
            **_base("analysis.completed"),
            job_id=new_id(),
            success=True,
            analysis_id="analysis-1",
            passed=True,
            score=0.85,
            refinement_loop=0,
        )
        assert e.success is True
        assert e.passed is True
        assert e.score == 0.85
        assert e.analysis_id == "analysis-1"
        assert e.refinement_loop == 0

    def test_failure_case_no_analysis_id(self) -> None:
        e = AnalysisCompletedEvent(
            **_base("analysis.completed"),
            job_id=new_id(),
            success=False,
            error_code="llm_schema_error",
            error_message="Validator returned wrong tool",
        )
        assert e.success is False
        assert e.analysis_id is None
        assert e.passed is False           # default
        assert e.score == 0.0              # default
        assert e.error_code == "llm_schema_error"

    def test_failed_validation_but_successful_completion(self) -> None:
        # Analysis ran successfully but validator score below threshold.
        e = AnalysisCompletedEvent(
            **_base("analysis.completed"),
            job_id=new_id(),
            success=True,
            analysis_id="a1",
            passed=False,
            score=0.62,
            refinement_loop=1,
        )
        assert e.success is True
        assert e.passed is False
        assert e.score == 0.62

    def test_roundtrip_json(self) -> None:
        e = AnalysisCompletedEvent(
            **_base("analysis.completed"),
            job_id=new_id(),
            success=True,
            analysis_id="a1",
            passed=True,
            score=0.9,
            refinement_loop=2,
        )
        restored = AnalysisCompletedEvent.model_validate_json(e.model_dump_json())
        assert restored.score == 0.9
        assert restored.passed is True
        assert restored.refinement_loop == 2

    def test_discriminated_union_dispatch(self) -> None:
        e = AnalysisCompletedEvent(
            **_base("analysis.completed"),
            job_id=new_id(),
            success=True,
            analysis_id="a1",
            passed=True,
            score=0.8,
        )
        adapter: TypeAdapter[AnyEvent] = TypeAdapter(AnyEvent)
        parsed = adapter.validate_json(e.model_dump_json())
        assert isinstance(parsed, AnalysisCompletedEvent)

    def test_no_legacy_validation_score_field(self) -> None:
        e = AnalysisCompletedEvent(
            **_base("analysis.completed"),
            job_id=new_id(),
            success=True,
        )
        # validation_score was renamed to score in Phase 8
        assert not hasattr(e, "validation_score")


@pytest.mark.unit
class TestIngestDocRef:
    def test_all_fields(self) -> None:
        ref = IngestDocRef(
            id="doc1", source_id="src1",
            content_hash="abc", blob_uri_raw="s3://b/k", char_count=100,
        )
        assert ref.id == "doc1"
        assert ref.blob_uri_raw == "s3://b/k"
        assert ref.char_count == 100

    def test_optional_fields_default_none_zero(self) -> None:
        ref = IngestDocRef(id="d1", source_id="s1", content_hash="h")
        assert ref.blob_uri_raw is None
        assert ref.char_count == 0

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValueError):
            IngestDocRef(id="d1", source_id="s1", content_hash="h", unknown="x")  # type: ignore[call-arg]
