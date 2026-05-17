"""Phase 8 graph integrity smoke tests.

Verifies that:
    - The graph still builds cleanly with the Phase 8 node changes.
    - All required nodes are registered.
    - With a kafka_producer in config and interrupt_after=[NODE_ANALYZE], the
      graph correctly pauses at NODE_ANALYZE without running publish_node.
    - The AnalysisPendingEvent gets published when the production path runs.

interrupt_after is a runtime astream() parameter (NOT compiled in), preserving
the unit-test stub-path compatibility documented in CLAUDE.md rule 21.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from langgraph.checkpoint.memory import MemorySaver

from trendstorm.agents.orchestrator.edges import (
    NODE_ANALYZE,
    NODE_EMBED,
    NODE_FAIL,
    NODE_INGEST,
    NODE_INIT,
    NODE_PUBLISH,
    NODE_REFINE,
    NODE_RETRIEVE,
)
from trendstorm.agents.orchestrator.graph import build_orchestrator_graph
from trendstorm.agents.stages import Stage
from trendstorm.agents.state import (
    JobState,
    ObservabilityContext,
    SourceRef,
)
from trendstorm.orchestration.events import AnalysisPendingEvent
from trendstorm.orchestration.topics import Topic
from trendstorm.shared.ids import new_id

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Graph integrity
# ---------------------------------------------------------------------------

class TestGraphIntegrity:
    def test_graph_builds_without_checkpointer(self) -> None:
        graph = build_orchestrator_graph()
        assert graph is not None

    def test_graph_builds_with_memory_checkpointer(self) -> None:
        graph = build_orchestrator_graph(MemorySaver())
        assert graph is not None

    def test_all_required_nodes_registered(self) -> None:
        graph = build_orchestrator_graph()
        registered = set(graph.get_graph().nodes.keys())
        for node in (
            NODE_INIT, NODE_INGEST, NODE_EMBED,
            NODE_RETRIEVE, NODE_ANALYZE, NODE_REFINE,
            NODE_PUBLISH, NODE_FAIL,
        ):
            assert node in registered, f"Missing node: {node}"


# ---------------------------------------------------------------------------
# Phase 8: production-path interrupt behaviour
# ---------------------------------------------------------------------------

def _initial_state() -> JobState:
    """Fresh PENDING state — the graph starts here."""
    return JobState(
        job_id=new_id(),
        tenant_id=new_id(),
        category_id=new_id(),
        sources=[SourceRef(id="s1", type="http", label="ex.com")],
        observability=ObservabilityContext(correlation_id="cid-1"),
    )


def _fake_producer() -> Any:
    p = MagicMock()
    p.send_and_wait = AsyncMock()
    return p


async def _drive_to_retrieving(graph: Any, state: JobState, thread_id: str) -> None:
    """Run the graph in stub mode (no producer) until paused after embed_node.

    Leaves state at stage=RETRIEVING with documents + chunks populated,
    so the next astream(None, producer_config, interrupt_after=[NODE_ANALYZE])
    can exercise the Phase 8 production path.
    """
    stub_config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
    async for _ in graph.astream(
        state, config=stub_config, interrupt_after=[NODE_EMBED]
    ):
        pass


class TestPhase8ProductionPathInterrupt:
    async def test_interrupt_after_analyze_pauses_graph(self) -> None:
        """With kafka_producer + interrupt_after=[NODE_ANALYZE], the graph
        must pause without running publish_node."""
        producer = _fake_producer()
        graph = build_orchestrator_graph(MemorySaver())
        state = _initial_state()
        await _drive_to_retrieving(graph, state, state.job_id)

        prod_config: dict[str, Any] = {
            "configurable": {
                "thread_id": state.job_id,
                "kafka_producer": producer,
            }
        }
        nodes_run: list[str] = []
        async for step in graph.astream(
            None, config=prod_config, interrupt_after=[NODE_ANALYZE]
        ):
            for node_name in step:
                if node_name != "__interrupt__":
                    nodes_run.append(node_name)

        assert NODE_RETRIEVE in nodes_run
        assert NODE_ANALYZE in nodes_run
        assert NODE_PUBLISH not in nodes_run

    async def test_paused_state_is_analyzing(self) -> None:
        producer = _fake_producer()
        graph = build_orchestrator_graph(MemorySaver())
        state = _initial_state()
        await _drive_to_retrieving(graph, state, state.job_id)

        prod_config: dict[str, Any] = {
            "configurable": {
                "thread_id": state.job_id,
                "kafka_producer": producer,
            }
        }
        async for _ in graph.astream(
            None, config=prod_config, interrupt_after=[NODE_ANALYZE]
        ):
            pass

        snapshot = await graph.aget_state(prod_config)
        paused = JobState.model_validate(snapshot.values)
        assert paused.stage == Stage.ANALYZING

    async def test_analysis_pending_event_published(self) -> None:
        producer = _fake_producer()
        graph = build_orchestrator_graph(MemorySaver())
        state = _initial_state()
        await _drive_to_retrieving(graph, state, state.job_id)

        prod_config: dict[str, Any] = {
            "configurable": {
                "thread_id": state.job_id,
                "kafka_producer": producer,
            }
        }
        async for _ in graph.astream(
            None, config=prod_config, interrupt_after=[NODE_ANALYZE]
        ):
            pass

        analysis_calls = [
            c for c in producer.send_and_wait.call_args_list
            if c.args[0] == Topic.ANALYSIS_PENDING.value
        ]
        assert len(analysis_calls) == 1
        event = AnalysisPendingEvent.model_validate_json(
            analysis_calls[0].kwargs["value"]
        )
        assert event.job_id == state.job_id
        assert event.category_id == state.category_id
        assert event.refinement_loop == 0
        assert event.refinement_notes is None

    async def test_no_analysis_state_after_pause(self) -> None:
        """Production analyze_node does not populate AnalysisState — the
        analyst worker injects it on resume via aupdate_state."""
        producer = _fake_producer()
        graph = build_orchestrator_graph(MemorySaver())
        state = _initial_state()
        await _drive_to_retrieving(graph, state, state.job_id)

        prod_config: dict[str, Any] = {
            "configurable": {
                "thread_id": state.job_id,
                "kafka_producer": producer,
            }
        }
        async for _ in graph.astream(
            None, config=prod_config, interrupt_after=[NODE_ANALYZE]
        ):
            pass

        snapshot = await graph.aget_state(prod_config)
        paused = JobState.model_validate(snapshot.values)
        assert paused.analysis.insights_doc_id is None
        assert paused.analysis.validation_passed is False
        assert paused.analysis.validation_score == 0.0

    async def test_resume_after_aupdate_state_reaches_publish_pause(self) -> None:
        """Simulates the full Phase 9 handoff: analyst injects result →
        graph resumes to publish_node (pauses) → publisher injects result →
        graph reaches COMPLETED.

        Phase 9: publish_node is also dual-mode — it publishes PublishPendingEvent
        and pauses via interrupt_after=[NODE_PUBLISH], matching the Phase 8
        analyze pattern.
        """
        producer = _fake_producer()
        graph = build_orchestrator_graph(MemorySaver())
        state = _initial_state()
        await _drive_to_retrieving(graph, state, state.job_id)

        prod_config: dict[str, Any] = {
            "configurable": {
                "thread_id": state.job_id,
                "kafka_producer": producer,
            }
        }
        # Pause at NODE_ANALYZE (production path publishes AnalysisPendingEvent).
        async for _ in graph.astream(
            None, config=prod_config, interrupt_after=[NODE_ANALYZE]
        ):
            pass

        # Analyst worker simulation: inject pass result + advance to PUBLISHING.
        from trendstorm.agents.state import AnalysisState
        await graph.aupdate_state(
            prod_config,
            {
                "stage": Stage.PUBLISHING,
                "analysis": AnalysisState(
                    insights_doc_id="ana-1",
                    validation_score=0.9,
                    validation_passed=True,
                ),
            },
            as_node=NODE_ANALYZE,
        )

        # Resume — graph runs publish_node (production path: publishes event + pauses).
        nodes_run: list[str] = []
        async for step in graph.astream(
            None, config=prod_config, interrupt_after=[NODE_PUBLISH]
        ):
            for node_name in step:
                if node_name != "__interrupt__":
                    nodes_run.append(node_name)

        assert NODE_PUBLISH in nodes_run
        # Graph is now paused at NODE_PUBLISH; stage is still PUBLISHING.
        snapshot = await graph.aget_state(prod_config)
        paused = JobState.model_validate(snapshot.values)
        assert paused.stage == Stage.PUBLISHING

        # Publisher worker simulation: inject report IDs + advance to COMPLETED.
        from trendstorm.agents.state import PublishingState
        await graph.aupdate_state(
            prod_config,
            {
                "stage": Stage.COMPLETED,
                "publishing": PublishingState(
                    report_doc_id="report-md-1",
                    report_blob_uri="s3://trendstorm-reports/job-1/report-md-1/report.md",
                ),
            },
            as_node=NODE_PUBLISH,
        )

        # Final resume: after_publish sees report_doc_id → END.
        async for _ in graph.astream(None, config=prod_config):
            pass

        snapshot = await graph.aget_state(prod_config)
        final = JobState.model_validate(snapshot.values)
        assert final.stage == Stage.COMPLETED
