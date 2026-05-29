"""Orchestrator StateGraph definition.

This is the actual LangGraph workflow. Nodes do work; edges route between
nodes; the graph orchestrates the whole thing with persistent checkpointing.

Why a builder function instead of a module-level graph instance?
    - Tests build a graph with a different checkpointer (in-memory).
    - Workers pass their own MongoSaver instance.
    - The graph is bound to a checkpointer, so we can't have a singleton.

Compile-time guarantees we get for free:
    - LangGraph validates that every node referenced in an edge exists.
    - Conditional-edge functions are type-checked at registration time.
    - START and END are reserved sentinels; using them wrong fails fast.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from langgraph.graph import END, START, StateGraph

from trendstorm.agents.orchestrator import edges
from trendstorm.agents.orchestrator import nodes as graph_nodes
from trendstorm.agents.orchestrator.edges import (
    NODE_ANALYZE,
    NODE_EMBED,
    NODE_END,
    NODE_FAIL,
    NODE_INGEST,
    NODE_INIT,
    NODE_MEMORY_CONSOLIDATION,
    NODE_PUBLISH,
    NODE_REFINE,
    NODE_RETRIEVE,
    NODE_REVIEW_GATE,
)
from trendstorm.agents.state import JobState

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph.state import CompiledStateGraph


def build_orchestrator_graph(
    checkpointer: BaseCheckpointSaver | None = None,  # type: ignore[type-arg]  # langgraph stubs
) -> CompiledStateGraph:  # type: ignore[type-arg]  # langgraph stubs
    """Build and compile the orchestrator graph.

    Args:
        checkpointer: Persistence backend. Pass MongoSaver in production;
            pass None or an InMemorySaver in tests. None means no
            persistence — workflows can't resume across restarts.

    Returns:
        A compiled graph ready for `await graph.ainvoke(state, config)`.

    """
    # JobState is a Pydantic model; LangGraph supports it via the schema arg.
    g: StateGraph = StateGraph(JobState)  # type: ignore[type-arg]  # langgraph stubs

    # ----- Register nodes ----------------------------------------------
    # Each registration: name -> async function. Names MUST match the
    # constants in edges.py.
    g.add_node(NODE_INIT, graph_nodes.init_job)
    g.add_node(NODE_INGEST, graph_nodes.ingest_node)
    g.add_node(NODE_EMBED, graph_nodes.embed_node)
    g.add_node(NODE_RETRIEVE, graph_nodes.retrieve_node)
    g.add_node(NODE_ANALYZE, graph_nodes.analyze_node)
    g.add_node(NODE_REFINE, graph_nodes.refine_node)
    g.add_node(NODE_REVIEW_GATE, graph_nodes.review_gate_node)
    g.add_node(NODE_PUBLISH, graph_nodes.publish_node)
    g.add_node(NODE_MEMORY_CONSOLIDATION, graph_nodes.memory_consolidation_node)
    g.add_node(NODE_FAIL, graph_nodes.fail_node)

    # ----- Entry point --------------------------------------------------
    g.add_edge(START, NODE_INIT)

    # ----- Unconditional edges -----------------------------------------
    # After init, always go to ingest (init has no failure mode).
    g.add_edge(NODE_INIT, NODE_INGEST)

    # After refine, always go back to retrieve.
    g.add_edge(NODE_REFINE, NODE_RETRIEVE)

    # FAIL is terminal.
    g.add_edge(NODE_FAIL, END)

    # ----- Conditional edges (the interesting part) --------------------
    # Each takes (state) -> next_node_name. The third arg is a mapping from
    # returned name to actual node — usually identity, but LangGraph allows
    # renaming.
    g.add_conditional_edges(
        NODE_INGEST,
        edges.after_ingest,
        {NODE_EMBED: NODE_EMBED, NODE_INGEST: NODE_INGEST, NODE_FAIL: NODE_FAIL},
    )
    g.add_conditional_edges(
        NODE_EMBED,
        edges.after_embed,
        {NODE_RETRIEVE: NODE_RETRIEVE, NODE_EMBED: NODE_EMBED, NODE_FAIL: NODE_FAIL},
    )
    g.add_conditional_edges(
        NODE_RETRIEVE,
        edges.after_retrieve,
        {NODE_ANALYZE: NODE_ANALYZE, NODE_RETRIEVE: NODE_RETRIEVE, NODE_FAIL: NODE_FAIL},
    )
    g.add_conditional_edges(
        NODE_ANALYZE,
        edges.after_analyze,
        {NODE_REVIEW_GATE: NODE_REVIEW_GATE, NODE_REFINE: NODE_REFINE, NODE_FAIL: NODE_FAIL},
    )
    g.add_conditional_edges(
        NODE_REVIEW_GATE,
        edges.after_review_gate,
        {NODE_PUBLISH: NODE_PUBLISH, NODE_END: END, NODE_FAIL: NODE_FAIL},
    )
    g.add_conditional_edges(
        NODE_PUBLISH,
        edges.after_publish,
        {
            NODE_MEMORY_CONSOLIDATION: NODE_MEMORY_CONSOLIDATION,
            NODE_PUBLISH: NODE_PUBLISH,
            NODE_FAIL: NODE_FAIL,
        },
    )
    g.add_conditional_edges(
        NODE_MEMORY_CONSOLIDATION,
        edges.after_memory_consolidation,
        {
            END: END,
            NODE_MEMORY_CONSOLIDATION: NODE_MEMORY_CONSOLIDATION,
            NODE_FAIL: NODE_FAIL,
        },
    )

    return g.compile(checkpointer=checkpointer)
