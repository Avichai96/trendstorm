"""Unit tests for AnalystWorker — idempotency key, handler logic, retry routing.

All dependencies (Analyst, repositories, Kafka producer) are mocked.
No Mongo, no Kafka, no LLM calls.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from trendstorm.domain.analyses.models import Analysis
from trendstorm.domain.categories.models import Category
from trendstorm.orchestration.events import (
    AnalysisCompletedEvent,
    AnalysisPendingEvent,
    IngestPendingEvent,
)
from trendstorm.orchestration.topics import Topic
from trendstorm.orchestration.workers.analyst_worker import AnalystWorker
from trendstorm.services.analysis.analyst import AnalysisResult
from trendstorm.services.analysis.validator import ValidationResult
from trendstorm.shared.errors import LLMPermanentError, ValidationError
from trendstorm.shared.ids import new_id

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_event(
    *,
    job_id: str | None = None,
    category_id: str | None = None,
    refinement_loop: int = 0,
    refinement_notes: str | None = None,
    attempt: int = 1,
    tenant_id: str = "t1",
) -> AnalysisPendingEvent:
    return AnalysisPendingEvent(
        correlation_id="cid",
        tenant_id=tenant_id,
        job_id=job_id or new_id(),
        category_id=category_id or new_id(),
        refinement_loop=refinement_loop,
        refinement_notes=refinement_notes,
        attempt=attempt,
    )


def _make_analysis(*, tenant_id: str, job_id: str, category_id: str) -> Analysis:
    return Analysis(
        tenant_id=tenant_id, job_id=job_id, category_id=category_id,
        summary="generated summary",
        insights=[],
        citations=[],
    )


def _make_result(*, score: float = 0.85, passed: bool = True, tenant_id: str = "t1") -> AnalysisResult:
    analysis = _make_analysis(tenant_id=tenant_id, job_id=new_id(), category_id=new_id())
    validation = ValidationResult(score=score, passed=passed, notes="n")
    return AnalysisResult(analysis=analysis, validation=validation)


def _build_worker(
    *,
    analyst_result: AnalysisResult | None = None,
    analyst_error: Exception | None = None,
    category: Category | None = None,
):
    """Build an AnalystWorker with all dependencies mocked."""
    analyst = MagicMock()
    if analyst_error is not None:
        analyst.produce_analysis = AsyncMock(side_effect=analyst_error)
    else:
        analyst.produce_analysis = AsyncMock(
            return_value=analyst_result or _make_result()
        )

    analysis_repo = MagicMock()
    analysis_repo.insert = AsyncMock()

    category_repo = MagicMock()
    category_repo.get = AsyncMock(return_value=category or Category(
        tenant_id="t1", name="cat", description="desc", keywords=[],
    ))

    idempotency = MagicMock()
    producer = MagicMock()
    producer.producer = MagicMock()
    producer.producer.send_and_wait = AsyncMock()

    _kafka_settings = MagicMock()

    # Patch BaseConsumer.__init__ to avoid real Kafka setup
    worker = AnalystWorker.__new__(AnalystWorker)
    worker._analyst = analyst
    worker._analysis_repo = analysis_repo
    worker._category_repo = category_repo
    worker._idempotency = idempotency
    worker._producer = producer
    worker._worker_name = "analyst"
    worker._ledger_repo = None
    return worker, analyst, analysis_repo, category_repo, producer


def _extract_published_completion(producer: Any) -> AnalysisCompletedEvent:
    """Find the AnalysisCompletedEvent among all send_and_wait calls."""
    for call in producer.producer.send_and_wait.call_args_list:
        topic = call.args[0]
        if topic == Topic.ANALYSIS_COMPLETED.value:
            raw = call.kwargs["value"]
            return AnalysisCompletedEvent.model_validate_json(raw)
    raise AssertionError(
        f"No {Topic.ANALYSIS_COMPLETED.value} call found in "
        f"{[c.args[0] for c in producer.producer.send_and_wait.call_args_list]}"
    )


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestAnalystWorkerIdempotencyKey:
    def test_key_includes_refinement_loop(self) -> None:
        worker, *_ = _build_worker()
        event = _make_event(job_id="j1", refinement_loop=0)
        assert worker._idempotency_key(event) == "analyst:j1:0"

    def test_different_loops_have_different_keys(self) -> None:
        worker, *_ = _build_worker()
        ev0 = _make_event(job_id="j1", refinement_loop=0)
        ev1 = _make_event(job_id="j1", refinement_loop=1)
        assert worker._idempotency_key(ev0) != worker._idempotency_key(ev1)

    def test_non_analysis_event_falls_back_to_event_id_key(self) -> None:
        worker, *_ = _build_worker()
        other = IngestPendingEvent(
            correlation_id="c", tenant_id="t", job_id="j", source_ids=[],
        )
        key = worker._idempotency_key(other)
        assert key is not None
        assert key.startswith("analyst:")


# ---------------------------------------------------------------------------
# Happy path handler
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestAnalystWorkerHandle:
    async def test_calls_analyst_with_correct_args(self) -> None:
        worker, analyst, *_ = _build_worker()
        event = _make_event(
            tenant_id="t1",
            category_id="cat1",
            refinement_loop=1,
            refinement_notes="address grounding",
        )
        await worker.handle(event)

        analyst.produce_analysis.assert_called_once()
        kwargs = analyst.produce_analysis.call_args.kwargs
        assert kwargs["tenant_id"] == "t1"
        assert kwargs["job_id"] == event.job_id
        assert kwargs["refinement_loop"] == 1
        assert kwargs["refinement_notes"] == "address grounding"

    async def test_persists_analysis_before_publishing(self) -> None:
        worker, _, analysis_repo, _, producer = _build_worker()
        await worker.handle(_make_event())
        analysis_repo.insert.assert_called_once()
        # send_and_wait is called multiple times (stream events + analysis.completed)
        assert producer.producer.send_and_wait.call_count >= 1
        # Verify analysis.completed is among them
        _extract_published_completion(producer)

    async def test_publishes_completion_with_validator_fields(self) -> None:
        result = _make_result(score=0.82, passed=True)
        worker, _, _, _, producer = _build_worker(analyst_result=result)
        event = _make_event(refinement_loop=0)
        await worker.handle(event)

        completed = _extract_published_completion(producer)
        assert completed.success is True
        assert completed.passed is True
        assert completed.score == 0.82
        assert completed.refinement_loop == 0
        assert completed.analysis_id == result.analysis.id

    async def test_refinement_loop_carries_through_to_completion(self) -> None:
        worker, _, _, _, producer = _build_worker()
        await worker.handle(_make_event(refinement_loop=2))
        completed = _extract_published_completion(producer)
        assert completed.refinement_loop == 2

    async def test_failed_validation_still_publishes_success_true(self) -> None:
        # passed=False but no exception → success=True, passed=False.
        result = _make_result(score=0.55, passed=False)
        worker, _, _, _, producer = _build_worker(analyst_result=result)
        await worker.handle(_make_event())
        completed = _extract_published_completion(producer)
        assert completed.success is True   # the analysis itself ran fine
        assert completed.passed is False   # but it didn't pass the validator

    async def test_event_uses_job_id_as_kafka_key(self) -> None:
        worker, _, _, _, producer = _build_worker()
        event = _make_event(job_id="job-xyz")
        await worker.handle(event)
        # Check all calls — all of them should use the job_id as key
        for call in producer.producer.send_and_wait.call_args_list:
            assert call.kwargs["key"] == b"job-xyz"

    async def test_correlation_id_propagated_to_completion(self) -> None:
        worker, _, _, _, producer = _build_worker()
        event = AnalysisPendingEvent(
            correlation_id="cid-unique-123",
            tenant_id="t1", job_id="j1", category_id="c1",
        )
        await worker.handle(event)
        completed = _extract_published_completion(producer)
        assert completed.correlation_id == "cid-unique-123"

    async def test_non_pending_event_ignored(self) -> None:
        worker, analyst, *_ = _build_worker()
        other = IngestPendingEvent(
            correlation_id="c", tenant_id="t", job_id="j", source_ids=[],
        )
        await worker.handle(other)
        analyst.produce_analysis.assert_not_called()


# ---------------------------------------------------------------------------
# Permanent-failure handling
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestAnalystWorkerPermanentFailures:
    async def test_llm_permanent_error_publishes_failure(self) -> None:
        worker, _, analysis_repo, _, producer = _build_worker(
            analyst_error=LLMPermanentError("auth failed")
        )
        await worker.handle(_make_event(refinement_loop=1))

        # Analysis is NOT persisted on permanent failure
        analysis_repo.insert.assert_not_called()
        completed = _extract_published_completion(producer)
        assert completed.success is False
        assert completed.passed is False
        assert completed.analysis_id is None
        assert completed.error_code == "llm_permanent_error"
        assert completed.refinement_loop == 1

    async def test_validation_error_publishes_failure(self) -> None:
        worker, _, _, _, producer = _build_worker(
            analyst_error=ValidationError("no chunks retrieved")
        )
        await worker.handle(_make_event())
        completed = _extract_published_completion(producer)
        assert completed.success is False
        assert "no chunks" in (completed.error_message or "")

    async def test_missing_category_publishes_failure(self) -> None:
        worker, analyst, _, category_repo, producer = _build_worker()
        category_repo.get = AsyncMock(return_value=None)
        await worker.handle(_make_event())
        analyst.produce_analysis.assert_not_called()
        completed = _extract_published_completion(producer)
        assert completed.success is False
        assert completed.error_code == "not_found"

    async def test_unexpected_exception_propagates_for_retry(self) -> None:
        # An exception NOT in the permanent set should bubble up so the
        # BaseConsumer retry topology handles it.
        worker, _, _, _, _ = _build_worker(
            analyst_error=RuntimeError("transient hiccup")
        )
        with pytest.raises(RuntimeError):
            await worker.handle(_make_event())
