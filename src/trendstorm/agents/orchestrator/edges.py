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
NODE_REVIEW_GATE = "review_gate"   # HITL: pause or pass-through before publish
NODE_PUBLISH = "publish"
NODE_MEMORY_CONSOLIDATION = "memory_consolidation"   # Phase 15.5: episodic + semantic write
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
    """Drive self-correction then hand off to the HITL review gate.

    Logic:
        - If analysis didn't pass AND we can auto-refine: loop via refine_node.
        - Otherwise (passed, or budget exhausted): proceed to review_gate_node
          which either passes through (HITL off/not-flagged) or pauses the job
          for human review (HITL on and flagged).
    """
    if not state.analysis.validation_passed and state.can_refine() and state.has_budget(Stage.ANALYZING):
        logger.info(
            "analysis_refine",
            job_id=state.job_id,
            score=state.analysis.validation_score,
            loop=state.refinement_loops,
        )
        return NODE_REFINE
    if not state.analysis.validation_passed:
        # Out of refinement loops. Forward to review gate; graceful degradation.
        logger.info("analysis_forward_to_review_gate_low_score",
                    job_id=state.job_id, score=state.analysis.validation_score)
    return NODE_REVIEW_GATE


def after_review_gate(state: JobState) -> str:
    """Route after review_gate_node based on the stage it set.

    PUBLISHING: HITL off, not flagged, or review was approved → proceed to publish.
    AWAITING_REVIEW: job paused for human review → graph will be interrupted here.
    FAILED: something went wrong during gate → fail the job.
    """
    if state.stage == Stage.PUBLISHING:
        return NODE_PUBLISH
    if state.stage == Stage.AWAITING_REVIEW:
        # The graph will be interrupted by interrupt_after=[NODE_REVIEW_GATE].
        # This branch only runs if the interrupt is not in effect (e.g. tests).
        return NODE_END
    return NODE_FAIL


def after_publish(state: JobState) -> str:
    if state.publishing.report_doc_id:
        return NODE_MEMORY_CONSOLIDATION
    if state.has_budget(Stage.PUBLISHING):
        return NODE_PUBLISH
    return NODE_FAIL


def after_memory_consolidation(state: JobState) -> str:
    """Route after memory_consolidation_node.

    Memory write is best-effort: even if it failed the job reaches COMPLETED.
    The budget check only retries transient failures (Chroma down, LLM timeout).
    """
    if state.stage == Stage.COMPLETED:
        return NODE_END
    if state.has_budget(Stage.MEMORY_CONSOLIDATION):
        return NODE_MEMORY_CONSOLIDATION
    # Budget exhausted or non-retriable error: graceful degradation to COMPLETED.
    # Memory failure must not fail the job — the user's report is already published.
    logger.info("memory_consolidation.budget_exhausted_graceful", job_id=state.job_id)
    return NODE_END
