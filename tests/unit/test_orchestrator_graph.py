"""Graph-level unit tests using LangGraph's in-memory checkpointer.

These tests build the actual graph and run it end-to-end with stub nodes.
No Mongo, no Kafka — just LangGraph + Pydantic state. The fastest possible
proof that the orchestration logic works.

For real-system tests (Kafka -> worker -> Mongo update), see
tests/integration/test_orchestrator_flow.py.
"""
from __future__ import annotations

import pytest

from trendstorm.agents.orchestrator.graph import build_orchestrator_graph
from trendstorm.agents.stages import Stage
from trendstorm.agents.state import (
    JobState,
    ObservabilityContext,
    SourceRef,
)
from trendstorm.shared.ids import new_id

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _initial_state() -> JobState:
    return JobState(
        job_id=new_id(),
        tenant_id=new_id(),
        category_id=new_id(),
        sources=[
            SourceRef(id="s1", type="http", label="example.com"),
            SourceRef(id="s2", type="http", label="another.com"),
        ],
        observability=ObservabilityContext(correlation_id="test-cid"),
    )


async def _run_to_terminal(graph, state, *, max_steps: int = 50) -> JobState:
    """Drive the graph and return the final state.

    Uses stream_mode="values" so each iteration yields the full accumulated
    state — no checkpointer needed to read back the final snapshot.
    """
    config = {"configurable": {"thread_id": state.job_id}}
    steps = 0
    last_snapshot: dict | None = None
    async for snapshot in graph.astream(state, config=config, stream_mode="values"):
        last_snapshot = snapshot
        steps += 1
        if steps > max_steps:
            raise RuntimeError(f"Graph did not terminate in {max_steps} steps")
    if last_snapshot is None:
        raise RuntimeError("Graph produced no output")
    return JobState.model_validate(last_snapshot)


async def test_happy_path_reaches_completed() -> None:
    """No checkpointer, no failures injected — should reach COMPLETED cleanly."""
    graph = build_orchestrator_graph()  # no checkpointer; one-shot run
    state = _initial_state()
    final = await _run_to_terminal(graph, state)
    assert final.stage == Stage.COMPLETED
    assert final.publishing.report_doc_id is not None
    assert len(final.ingestion.raw_documents) == len(state.sources)


async def test_refinement_loop_executes_when_score_is_low() -> None:
    """Stubbed validation_score starts at 0.6 < 0.75, so we should refine
    at least once before passing on attempt 2 (score = 0.75) or 3 (0.9)."""
    graph = build_orchestrator_graph()
    state = _initial_state()
    final = await _run_to_terminal(graph, state)
    # First attempt scored 0.6, so refinement_loops increments before publishing.
    assert final.refinement_loops >= 1
    assert final.stage == Stage.COMPLETED


async def test_stage_progression_records_attempts() -> None:
    graph = build_orchestrator_graph()
    state = _initial_state()
    final = await _run_to_terminal(graph, state)
    # Every work stage must have at least one attempt recorded.
    assert final.attempts.get(Stage.INGESTING, 0) >= 1
    assert final.attempts.get(Stage.EMBEDDING, 0) >= 1
    assert final.attempts.get(Stage.RETRIEVING, 0) >= 1
    assert final.attempts.get(Stage.ANALYZING, 0) >= 1
    assert final.attempts.get(Stage.PUBLISHING, 0) >= 1


async def test_publishes_with_low_score_when_refinement_cap_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If we cap refinement aggressively, the graph must STILL publish.

    This is graceful degradation: better a low-confidence report than none.
    """
    from trendstorm.agents import state as state_module

    monkeypatch.setattr(state_module, "MAX_REFINEMENT_LOOPS", 0)

    graph = build_orchestrator_graph()
    state = _initial_state()
    final = await _run_to_terminal(graph, state)
    # Even with zero refinement budget, we must publish.
    assert final.stage == Stage.COMPLETED
    assert final.publishing.report_doc_id is not None
