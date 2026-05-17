"""Unit tests for Phase 8 production paths in retrieve_node and analyze_node.

The stub paths are exercised by test_orchestrator_graph.py end-to-end.
These tests target the production paths specifically — verifying that
when a kafka_producer is injected, the nodes publish events and return
the right partial state.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from trendstorm.agents.orchestrator.nodes import analyze_node, retrieve_node
from trendstorm.agents.stages import Stage
from trendstorm.agents.state import ChunkRef, JobState, KnowledgeState, SourceRef
from trendstorm.orchestration.events import AnalysisPendingEvent
from trendstorm.orchestration.topics import Topic

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(
    *,
    stage: Stage = Stage.RETRIEVING,
    refinement_loops: int = 0,
    n_chunks: int = 3,
) -> JobState:
    state = JobState.initial(
        tenant_id="t1",
        category_id="cat-1",
        sources=[SourceRef(id="s1", type="http", label="s1")],
        correlation_id="cid-1",
    )
    return state.model_copy(update={
        "job_id": "j1",
        "stage": stage,
        "refinement_loops": refinement_loops,
        "knowledge": KnowledgeState(
            chunk_refs=[ChunkRef(id=f"c{i}", document_id=f"d{i}") for i in range(n_chunks)],
            embedding_model="fake",
        ),
    })


def _fake_producer() -> Any:
    p = MagicMock()
    p.send_and_wait = AsyncMock()
    return p


def _production_config(producer: Any) -> dict[str, Any]:
    return {"configurable": {"kafka_producer": producer}}


def _stub_config() -> dict[str, Any]:
    return {"configurable": {}}


# ---------------------------------------------------------------------------
# retrieve_node — production path
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestRetrieveNodeProductionPath:
    async def test_no_kafka_calls_in_production(self) -> None:
        # retrieve_node is a pass-through in production — it does NOT publish.
        producer = _fake_producer()
        state = _make_state()
        await retrieve_node(state, _production_config(producer))
        producer.send_and_wait.assert_not_called()

    async def test_returns_chunk_ids_from_knowledge(self) -> None:
        producer = _fake_producer()
        state = _make_state(n_chunks=5)
        update = await retrieve_node(state, _production_config(producer))
        assert update["retrieval"].retrieved_chunk_ids == ["c0", "c1", "c2", "c3", "c4"]

    async def test_transitions_to_analyzing(self) -> None:
        producer = _fake_producer()
        state = _make_state()
        update = await retrieve_node(state, _production_config(producer))
        assert update["stage"] == Stage.ANALYZING

    async def test_query_marker_is_delegated(self) -> None:
        producer = _fake_producer()
        update = await retrieve_node(_make_state(), _production_config(producer))
        # The "query" field is just a marker — the analyst worker computes
        # the real query from the category brief.
        assert "delegated" in update["retrieval"].query.lower()

    async def test_refinement_count_reflects_loops(self) -> None:
        producer = _fake_producer()
        state = _make_state(refinement_loops=2)
        update = await retrieve_node(state, _production_config(producer))
        assert update["retrieval"].refinement_count == 2

    async def test_attempt_incremented(self) -> None:
        producer = _fake_producer()
        state = _make_state()
        update = await retrieve_node(state, _production_config(producer))
        assert update["attempts"][Stage.RETRIEVING] == 1


# ---------------------------------------------------------------------------
# analyze_node — production path
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestAnalyzeNodeProductionPath:
    async def test_publishes_analysis_pending_event(self) -> None:
        producer = _fake_producer()
        state = _make_state(stage=Stage.ANALYZING)
        await analyze_node(state, _production_config(producer))
        producer.send_and_wait.assert_called_once()
        args = producer.send_and_wait.call_args
        assert args.args[0] == Topic.ANALYSIS_PENDING.value
        assert args.kwargs["key"] == b"j1"

    async def test_published_event_has_correct_fields(self) -> None:
        producer = _fake_producer()
        state = _make_state(stage=Stage.ANALYZING, refinement_loops=0)
        await analyze_node(state, _production_config(producer))
        raw = producer.send_and_wait.call_args.kwargs["value"]
        event = AnalysisPendingEvent.model_validate_json(raw)
        assert event.job_id == "j1"
        assert event.category_id == "cat-1"
        assert event.tenant_id == "t1"
        assert event.refinement_loop == 0
        assert event.refinement_notes is None
        assert event.correlation_id == "cid-1"

    async def test_does_not_set_analysis_state_in_production(self) -> None:
        # In production we delegate analysis; the worker injects the
        # AnalysisState later via aupdate_state(as_node=NODE_ANALYZE).
        producer = _fake_producer()
        state = _make_state(stage=Stage.ANALYZING)
        update = await analyze_node(state, _production_config(producer))
        assert "analysis" not in update

    async def test_does_not_transition_stage(self) -> None:
        # Stage stays ANALYZING; aupdate_state moves it later.
        producer = _fake_producer()
        state = _make_state(stage=Stage.ANALYZING)
        update = await analyze_node(state, _production_config(producer))
        assert "stage" not in update

    async def test_attempt_incremented_in_production(self) -> None:
        producer = _fake_producer()
        state = _make_state(stage=Stage.ANALYZING)
        update = await analyze_node(state, _production_config(producer))
        assert update["attempts"][Stage.ANALYZING] == 1

    async def test_initial_pass_has_no_refinement_notes(self) -> None:
        producer = _fake_producer()
        state = _make_state(stage=Stage.ANALYZING, refinement_loops=0)
        await analyze_node(state, _production_config(producer))
        event = AnalysisPendingEvent.model_validate_json(
            producer.send_and_wait.call_args.kwargs["value"]
        )
        assert event.refinement_notes is None


# ---------------------------------------------------------------------------
# Stub-path regression — ensure prior behavior preserved
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestNodeStubPathRegression:
    async def test_retrieve_node_stub_returns_half_chunks(self) -> None:
        state = _make_state(n_chunks=4)
        update = await retrieve_node(state, _stub_config())
        # Stub path: half the chunks (n_chunks // 2 = 2)
        assert len(update["retrieval"].retrieved_chunk_ids) == 2
        assert update["stage"] == Stage.ANALYZING

    async def test_analyze_node_stub_returns_synthetic_analysis(self) -> None:
        state = _make_state(stage=Stage.ANALYZING, refinement_loops=0)
        update = await analyze_node(state, _stub_config())
        assert "analysis" in update
        # First-attempt score is 0.6 (mediocre), exercises refine loop
        assert update["analysis"].validation_score == pytest.approx(0.6)
        assert update["analysis"].validation_passed is False

    async def test_analyze_node_stub_passes_after_refinement(self) -> None:
        # Refinement #1: score = 0.6 + 0.15 = 0.75 → passes
        state = _make_state(stage=Stage.ANALYZING, refinement_loops=1)
        update = await analyze_node(state, _stub_config())
        assert update["analysis"].validation_score == pytest.approx(0.75)
        assert update["analysis"].validation_passed is True
