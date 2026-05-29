"""Unit tests for OrchestratorWorker._handle_analysis_completed.

All graph and Mongo/Kafka interactions are mocked. We verify routing logic:
    - permanent failures fail the job
    - passed/budget-exhausted advances to publishing and streams the graph
    - failed-with-budget triggers a refinement republish with validator notes
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from trendstorm.agents.stages import Stage
from trendstorm.agents.state import JobState, SourceRef
from trendstorm.orchestration.events import (
    AnalysisCompletedEvent,
    AnalysisPendingEvent,
    PublishCompletedEvent,
)
from trendstorm.orchestration.topics import Topic
from trendstorm.orchestration.workers.orchestrator_worker import OrchestratorWorker
from trendstorm.shared.config import AnalysisSettings
from trendstorm.shared.types import JobStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _settings(max_refinement_loops: int = 2) -> AnalysisSettings:
    return AnalysisSettings(
        retrieval_k=50, rerank_k=30, final_k=10,
        query_expansion_count=3, validator_threshold=0.75,
        max_refinement_loops=max_refinement_loops,
    )


def _make_state(*, stage: Stage = Stage.ANALYZING, category_id: str = "cat-1") -> JobState:
    return JobState.initial(
        tenant_id="t1",
        category_id=category_id,
        sources=[SourceRef(id="s1", type="http", label="s1")],
        correlation_id="cid",
    ).model_copy(update={"job_id": "j1", "stage": stage})


def _make_completed_event(
    *,
    success: bool = True,
    passed: bool = True,
    score: float = 0.85,
    refinement_loop: int = 0,
    analysis_id: str | None = "ana-1",
    error_code: str | None = None,
    error_message: str | None = None,
) -> AnalysisCompletedEvent:
    return AnalysisCompletedEvent(
        correlation_id="cid",
        tenant_id="t1",
        job_id="j1",
        success=success,
        analysis_id=analysis_id,
        passed=passed,
        score=score,
        refinement_loop=refinement_loop,
        error_code=error_code,
        error_message=error_message,
    )


def _build_worker(
    *,
    state: JobState | None = None,
    validator_notes: str | None = "fix grounding",
    max_loops: int = 2,
):
    """Build an OrchestratorWorker with all dependencies mocked."""
    worker = OrchestratorWorker.__new__(OrchestratorWorker)
    worker._analysis_settings = _settings(max_refinement_loops=max_loops)
    worker._worker_name = "orchestrator"

    # Graph mock — supports aget_state, aupdate_state, astream
    graph = MagicMock()
    snapshot = MagicMock()
    snapshot.values = state.model_dump() if state else {}
    graph.aget_state = AsyncMock(return_value=snapshot)
    graph.aupdate_state = AsyncMock()

    async def _empty_stream(*args: Any, **kwargs: Any):
        if False:
            yield None  # pragma: no cover
        return

    graph.astream = MagicMock(return_value=_empty_stream())
    worker._graph = graph

    # Mongo: jobs and analyses
    jobs = MagicMock()
    jobs.update_status = AsyncMock()
    worker._jobs = jobs

    analyses = MagicMock()
    if validator_notes is not None:
        prior = MagicMock()
        prior.validator_notes = validator_notes
        analyses.get = AsyncMock(return_value=prior)
    else:
        analyses.get = AsyncMock(return_value=None)
    worker._analyses = analyses

    # Producer
    producer = MagicMock()
    producer.producer = MagicMock()
    producer.producer.send_and_wait = AsyncMock()
    worker._producer = producer

    return worker, graph, jobs, analyses, producer


def _published_pending(producer: Any) -> AnalysisPendingEvent:
    """Find the AnalysisPendingEvent on the producer mock."""
    for call in producer.producer.send_and_wait.call_args_list:
        if call.args[0] == Topic.ANALYSIS_PENDING.value:
            return AnalysisPendingEvent.model_validate_json(call.kwargs["value"])
    raise AssertionError("No AnalysisPendingEvent was published")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestAnalysisCompletedHandler:
    async def test_passed_advances_to_publishing_and_streams_graph(self) -> None:
        worker, graph, _, _, producer = _build_worker(state=_make_state())
        event = _make_completed_event(passed=True, score=0.9, refinement_loop=0)

        await worker._handle_analysis_completed(event)

        # State updated with stage=PUBLISHING
        graph.aupdate_state.assert_called_once()
        update_dict = graph.aupdate_state.call_args.args[1]
        assert update_dict["stage"] == Stage.PUBLISHING
        assert update_dict["analysis"].validation_score == 0.9
        assert update_dict["analysis"].validation_passed is True

        # No refinement event published
        for call in producer.producer.send_and_wait.call_args_list:
            assert call.args[0] != Topic.ANALYSIS_PENDING.value

        # Graph was streamed forward to completion
        graph.astream.assert_called_once()

    async def test_failed_with_budget_remaining_publishes_refinement(self) -> None:
        worker, graph, _, _, producer = _build_worker(
            state=_make_state(),
            validator_notes="address ungrounded claim about X",
            max_loops=2,
        )
        event = _make_completed_event(passed=False, score=0.55, refinement_loop=0)

        await worker._handle_analysis_completed(event)

        # State refinement_loops bumped
        update_dict = graph.aupdate_state.call_args.args[1]
        assert update_dict["refinement_loops"] == 1

        # AnalysisPendingEvent published with validator notes
        pending = _published_pending(producer)
        assert pending.refinement_loop == 1
        assert pending.refinement_notes == "address ungrounded claim about X"
        assert pending.category_id == "cat-1"
        assert pending.job_id == "j1"

        # Graph NOT streamed forward (waiting for next analyst pass)
        graph.astream.assert_not_called()

    async def test_failed_at_budget_exhausted_advances_anyway(self) -> None:
        # max_refinement_loops=2, current refinement_loop=2 → exhausted.
        worker, graph, _, _, producer = _build_worker(
            state=_make_state(), max_loops=2,
        )
        event = _make_completed_event(passed=False, score=0.6, refinement_loop=2)

        await worker._handle_analysis_completed(event)

        # Advances to publishing despite not passing — graceful degradation.
        update_dict = graph.aupdate_state.call_args.args[1]
        assert update_dict["stage"] == Stage.PUBLISHING
        graph.astream.assert_called_once()
        # No refinement event published
        for call in producer.producer.send_and_wait.call_args_list:
            assert call.args[0] != Topic.ANALYSIS_PENDING.value

    async def test_permanent_failure_fails_the_job(self) -> None:
        worker, graph, jobs, _, _ = _build_worker(state=_make_state())
        event = _make_completed_event(
            success=False, analysis_id=None,
            error_code="llm_permanent_error",
            error_message="auth failed",
        )

        await worker._handle_analysis_completed(event)

        jobs.update_status.assert_called_once()
        kwargs = jobs.update_status.call_args.kwargs
        assert kwargs["failure_code"] == "llm_permanent_error"
        # Graph NOT advanced or streamed
        graph.aupdate_state.assert_not_called()
        graph.astream.assert_not_called()

    async def test_terminal_state_short_circuits(self) -> None:
        worker, graph, jobs, _, _ = _build_worker(state=_make_state(stage=Stage.COMPLETED))
        event = _make_completed_event()
        await worker._handle_analysis_completed(event)
        graph.aupdate_state.assert_not_called()
        graph.astream.assert_not_called()
        jobs.update_status.assert_not_called()

    async def test_no_checkpoint_short_circuits(self) -> None:
        worker, graph, *_ = _build_worker(state=None)
        event = _make_completed_event()
        await worker._handle_analysis_completed(event)
        graph.aupdate_state.assert_not_called()

    async def test_refinement_uses_fallback_notes_when_no_analysis_doc(self) -> None:
        worker, _, _, _, producer = _build_worker(
            state=_make_state(), validator_notes=None,
        )
        # analyses.get returns None — no persisted Analysis to extract notes from.
        event = _make_completed_event(passed=False, score=0.5, refinement_loop=0)
        await worker._handle_analysis_completed(event)

        pending = _published_pending(producer)
        assert pending.refinement_notes is not None
        assert len(pending.refinement_notes) > 0  # fallback notes are used

    async def test_refinement_uses_fallback_when_analysis_id_missing(self) -> None:
        worker, _, _, _, producer = _build_worker(state=_make_state())
        # analysis_id is None on the event (shouldn't happen on success=True,
        # but be defensive)
        event = _make_completed_event(
            passed=False, score=0.5, refinement_loop=0, analysis_id=None,
        )
        await worker._handle_analysis_completed(event)
        pending = _published_pending(producer)
        # Fallback notes present
        assert pending.refinement_notes is not None
        assert len(pending.refinement_notes) > 0

    async def test_refinement_event_uses_job_id_as_kafka_key(self) -> None:
        worker, _, _, _, producer = _build_worker(state=_make_state())
        event = _make_completed_event(passed=False, score=0.5, refinement_loop=0)
        await worker._handle_analysis_completed(event)

        call = next(
            c for c in producer.producer.send_and_wait.call_args_list
            if c.args[0] == Topic.ANALYSIS_PENDING.value
        )
        assert call.kwargs["key"] == b"j1"

    async def test_dispatcher_routes_analysis_completed(self) -> None:
        """handle() must dispatch AnalysisCompletedEvent to the analysis handler."""
        worker, *_ = _build_worker(state=_make_state())
        worker._handle_analysis_completed = AsyncMock()  # type: ignore[method-assign]
        worker._handle_job_requested = AsyncMock()        # type: ignore[method-assign]
        event = _make_completed_event()
        await worker.handle(event)
        worker._handle_analysis_completed.assert_called_once_with(event)
        worker._handle_job_requested.assert_not_called()


def _make_publish_completed(
    *,
    success: bool = True,
    job_id: str = "j1",
    markdown_report_id: str | None = "md-1",
    pdf_report_id: str | None = "pdf-1",
    json_report_id: str | None = "json-1",
    error_code: str | None = None,
    error_message: str | None = None,
) -> PublishCompletedEvent:
    return PublishCompletedEvent(
        correlation_id="cid",
        tenant_id="t1",
        job_id=job_id,
        success=success,
        markdown_report_id=markdown_report_id,
        pdf_report_id=pdf_report_id,
        json_report_id=json_report_id,
        error_code=error_code,
        error_message=error_message,
    )


@pytest.mark.unit
class TestPublishCompletedHandler:
    """Tests for OrchestratorWorker._handle_publish_completed."""

    async def test_success_injects_publishing_state_and_advances_to_memory_consolidation(self) -> None:
        """Phase 15.5: successful publish routes to MEMORY_CONSOLIDATION, not COMPLETED."""
        worker, graph, _, _, _ = _build_worker(state=_make_state(stage=Stage.PUBLISHING))
        event = _make_publish_completed(success=True)

        await worker._handle_publish_completed(event)

        graph.aupdate_state.assert_called_once()
        update = graph.aupdate_state.call_args.args[1]
        assert update["stage"] == Stage.MEMORY_CONSOLIDATION
        assert update["publishing"].report_doc_id == "md-1"

        # Graph streamed (paused at memory_consolidation node)
        graph.astream.assert_called_once()

    async def test_success_does_not_update_status_to_completed_immediately(self) -> None:
        """Phase 15.5: publish success routes to MEMORY_CONSOLIDATION; COMPLETED comes later."""
        worker, graph, jobs, _, _ = _build_worker(state=_make_state(stage=Stage.PUBLISHING))
        await worker._handle_publish_completed(_make_publish_completed())
        # update_status is NOT called to COMPLETED here — that happens in _handle_memory_completed.
        # Only a failure path would call update_status from this handler on success.
        for call in jobs.update_status.call_args_list:
            status_arg = call.args[2] if len(call.args) > 2 else None
            assert status_arg != JobStatus.COMPLETED, "Should not advance to COMPLETED from publish handler"

    async def test_failure_marks_job_failed_without_resuming(self) -> None:
        worker, graph, jobs, _, _ = _build_worker(state=_make_state(stage=Stage.PUBLISHING))
        event = _make_publish_completed(
            success=False,
            markdown_report_id=None,
            error_code="not_found",
            error_message="analysis missing",
        )

        await worker._handle_publish_completed(event)

        jobs.update_status.assert_called_once()
        kwargs = jobs.update_status.call_args.kwargs
        assert kwargs["failure_code"] == "not_found"
        graph.aupdate_state.assert_not_called()
        graph.astream.assert_not_called()

    async def test_terminal_state_short_circuits(self) -> None:
        worker, graph, jobs, _, _ = _build_worker(state=_make_state(stage=Stage.COMPLETED))
        await worker._handle_publish_completed(_make_publish_completed())
        graph.aupdate_state.assert_not_called()
        graph.astream.assert_not_called()
        jobs.update_status.assert_not_called()

    async def test_no_checkpoint_short_circuits(self) -> None:
        worker, graph, *_ = _build_worker(state=None)
        await worker._handle_publish_completed(_make_publish_completed())
        graph.aupdate_state.assert_not_called()

    async def test_pdf_none_still_advances_to_memory_consolidation(self) -> None:
        worker, graph, _, _, _ = _build_worker(state=_make_state(stage=Stage.PUBLISHING))
        event = _make_publish_completed(pdf_report_id=None)
        await worker._handle_publish_completed(event)
        update = graph.aupdate_state.call_args.args[1]
        assert update["stage"] == Stage.MEMORY_CONSOLIDATION
        assert update["publishing"].report_doc_id == "md-1"

    async def test_dispatcher_routes_publish_completed(self) -> None:
        worker, *_ = _build_worker(state=_make_state())
        worker._handle_publish_completed = AsyncMock()  # type: ignore[method-assign]
        event = _make_publish_completed()
        await worker.handle(event)
        worker._handle_publish_completed.assert_called_once_with(event)


@pytest.mark.unit
class TestOrchestratorSubscriptions:
    """Verifies the orchestrator subscribes to all five topics needed in Phase 9."""

    def test_subscribed_topic_list(self) -> None:
        import inspect

        from trendstorm.orchestration.workers import orchestrator_worker as mod
        src = inspect.getsource(mod.OrchestratorWorker.__init__)
        assert "Topic.JOBS_REQUESTED" in src
        assert "Topic.INGEST_COMPLETED" in src
        assert "Topic.KNOWLEDGE_COMPLETED" in src
        assert "Topic.ANALYSIS_COMPLETED" in src
        assert "Topic.PUBLISH_COMPLETED" in src
