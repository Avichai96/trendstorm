"""Conditional edge functions for the orchestrator graph.

A conditional edge in LangGraph is a function `(state) -> str` that returns
the name of the next node. This is where business logic about WHAT happens
next lives — not in the nodes themselves.

Separating routing from work has big benefits:
    - Easy to change retry/refinement policy without touching node code.
    - Routing logic is small, pure, testable.
    - Visualization tools can render the graph structure clearly.

Convention: each function is named `after_<stage>` for symmetry with nodes.
"""
from __future__ import annotations

from trendstorm.agents.stages import Stage
from trendstorm.agents.state import JobState
from trendstorm.shared.logging import get_logger

logger = get_logger(__name__)


# Node name constants — these MUST match the keys used when adding nodes
# in graph.py. Centralized here so a typo fails fast at graph build time
# (LangGraph validates edges against registered node names).
NODE_INIT = "init_job"
NODE_INGEST = "ingest"
NODE_EMBED = "embed"
NODE_RETRIEVE = "retrieve"
NODE_ANALYZE = "analyze"
NODE_REFINE = "refine"
NODE_PUBLISH = "publish"
NODE_FAIL = "fail"
NODE_END = "__end__"   # LangGraph's reserved terminal node


# ===========================================================================
# After-stage routing functions
# ===========================================================================

def after_ingest(state: JobState) -> str:
    """Check if ingestion succeeded and whether we have budget to retry."""
    if state.ingestion.raw_documents:
        return NODE_EMBED
    if state.has_budget(Stage.INGESTING):
        return NODE_INGEST    # retry in-place
    return NODE_FAIL


def after_embed(state: JobState) -> str:
    if state.knowledge.chunk_refs:
        return NODE_RETRIEVE
    if state.has_budget(Stage.EMBEDDING):
        return NODE_EMBED
    return NODE_FAIL


def after_retrieve(state: JobState) -> str:
    if state.retrieval.retrieved_chunk_ids:
        return NODE_ANALYZE
    if state.has_budget(Stage.RETRIEVING):
        return NODE_RETRIEVE
    return NODE_FAIL


def after_analyze(state: JobState) -> str:
    """Drive self-correction — the most interesting edge in the graph.

    Logic:
        - If analysis passed validation: go publish.
        - If analysis didn't pass AND we can refine: loop back via refine_node
          which clears retrieval state and bumps the refinement counter.
        - Otherwise: ship the (possibly-mediocre) result anyway — better to
          give a partial answer than fail outright.
    """
    if state.analysis.validation_passed:
        return NODE_PUBLISH
    if state.can_refine() and state.has_budget(Stage.ANALYZING):
        logger.info(
            "analysis_refine",
            job_id=state.job_id,
            score=state.analysis.validation_score,
            loop=state.refinement_loops,
        )
        return NODE_REFINE
    # Out of refinement loops. Publish anyway; the report will note the
    # low confidence score. Future: configurable "strict mode" that fails.
    logger.info("analysis_publish_with_low_score", job_id=state.job_id,
                score=state.analysis.validation_score)
    return NODE_PUBLISH


def after_publish(state: JobState) -> str:
    if state.publishing.report_doc_id:
        return NODE_END
    if state.has_budget(Stage.PUBLISHING):
        return NODE_PUBLISH
    return NODE_FAIL
