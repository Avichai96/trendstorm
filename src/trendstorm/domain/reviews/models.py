"""Human-in-the-loop review domain models.

A ReviewRequest is created when review_gate_node determines a job's analysis
needs human approval before publication. It is resolved (approved / rejected /
refinement_requested / timed_out) by a reviewer via the API or by the timeout
sweeper worker.

Status lifecycle:
    PENDING → APPROVED          (reviewer approves; job proceeds to publishing)
    PENDING → REJECTED          (reviewer declines; job terminates as REJECTED)
    PENDING → REFINEMENT_REQUESTED  (reviewer requests another analyst pass)
    PENDING → TIMED_OUT         (sweeper fires at timeout_at; treated as rejected)

The decision_comment is forwarded to the next AnalysisPendingEvent as
refinement_notes when decision is REFINEMENT_REQUESTED.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field
from trendstorm_shared import FlaggingReason

from trendstorm.shared.ids import new_id


class ReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REFINEMENT_REQUESTED = "refinement_requested"
    TIMED_OUT = "timed_out"

    @property
    def is_resolved(self) -> bool:
        return self != ReviewStatus.PENDING


class ReviewDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_REFINEMENT = "request_refinement"


class ReviewRequest(BaseModel):
    """A single HITL review record. Append-only after creation."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    tenant_id: str
    job_id: str
    analysis_id: str
    stage_under_review: str  # Stage value at review creation time
    status: ReviewStatus = ReviewStatus.PENDING
    reviewer_id: str | None = None  # key_id or subject of the resolving principal
    decision_comment: str | None = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    resolved_at: datetime | None = None
    timeout_at: datetime  # absolute UTC deadline; sweeper fires here
    sla_seconds: int  # timeout window in seconds (stored for audit)

    # Populated by review_gate_node at creation time (Phase 15.6)
    validator_score: float | None = None
    refinement_loops_used: int = 0
    cost_usd_so_far_cents: int = 0  # integer cents; avoids float decimal issues
    flagging_reason: FlaggingReason | None = None

    @property
    def is_expired(self) -> bool:
        return datetime.now(UTC) >= self.timeout_at and self.status == ReviewStatus.PENDING
