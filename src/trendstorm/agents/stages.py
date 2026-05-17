"""Stages of the orchestrator workflow.

A Stage represents a major step in job processing. Stages are coarser than
LangGraph nodes — a single stage may contain multiple nodes (e.g. the
ANALYZING stage has analyze → validate → maybe-refine nodes).

Why a Stage enum separate from JobStatus?
    - JobStatus is the user-visible field exposed in the API; it might
      collapse multiple internal stages into one ("processing").
    - Stage is the internal state-machine vocabulary used by the graph.
    - This separation lets us refactor internals without breaking the API.

Transition rules
    PENDING     -> INGESTING
    INGESTING   -> EMBEDDING | FAILED
    EMBEDDING   -> RETRIEVING | FAILED
    RETRIEVING  -> ANALYZING | FAILED
    ANALYZING   -> PUBLISHING | RETRIEVING (refinement loop) | FAILED
    PUBLISHING  -> COMPLETED | FAILED
    Any         -> CANCELLED (user-initiated)

These are enforced in `is_valid_transition` so bugs in node code surface fast.
"""
from __future__ import annotations

from enum import StrEnum


class Stage(StrEnum):
    """Internal stage names used by the orchestrator graph."""

    PENDING = "pending"
    INGESTING = "ingesting"
    EMBEDDING = "embedding"
    RETRIEVING = "retrieving"
    ANALYZING = "analyzing"
    PUBLISHING = "publishing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {Stage.COMPLETED, Stage.FAILED, Stage.CANCELLED}


# Allowed forward transitions. Map of stage -> set of next stages.
# Cancellation can happen from any non-terminal stage; handled separately.
_TRANSITIONS: dict[Stage, frozenset[Stage]] = {
    Stage.PENDING:    frozenset({Stage.INGESTING, Stage.FAILED, Stage.CANCELLED}),
    Stage.INGESTING:  frozenset({Stage.INGESTING, Stage.EMBEDDING, Stage.FAILED, Stage.CANCELLED}),
    Stage.EMBEDDING:  frozenset({Stage.EMBEDDING, Stage.RETRIEVING, Stage.FAILED, Stage.CANCELLED}),
    Stage.RETRIEVING: frozenset({Stage.RETRIEVING, Stage.ANALYZING, Stage.FAILED, Stage.CANCELLED}),
    # Note: ANALYZING can loop back to RETRIEVING for self-correction
    Stage.ANALYZING:  frozenset({
        Stage.ANALYZING, Stage.PUBLISHING, Stage.RETRIEVING, Stage.FAILED, Stage.CANCELLED,
    }),
    Stage.PUBLISHING: frozenset({Stage.PUBLISHING, Stage.COMPLETED, Stage.FAILED, Stage.CANCELLED}),
    Stage.COMPLETED:  frozenset(),
    Stage.FAILED:     frozenset(),
    Stage.CANCELLED:  frozenset(),
}


def allowed_next_stages(stage: Stage) -> frozenset[Stage]:
    """Return the set of stages reachable from *stage*."""
    return _TRANSITIONS.get(stage, frozenset())


def is_valid_transition(from_stage: Stage, to_stage: Stage) -> bool:
    """Check if a stage transition is allowed.

    Catches programming errors (e.g. jumping from PENDING straight to
    ANALYZING). The graph code asserts this in development; in production
    we log and force-fail the job rather than crashing.
    """
    return to_stage in _TRANSITIONS.get(from_stage, frozenset())
