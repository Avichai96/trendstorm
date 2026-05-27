"""Domain models for per-job SSE stream events.

Stream events flow:
    Worker publishes stream.partial.v1 Kafka event
    → SSE Coordinator consumes, assigns seq, writes to Redis Streams + Pub/Sub
    → SSE endpoint reads Redis and forwards to connected client

Every event carries a ULID event_id (global dedup key) and a job-scoped
monotonic seq (Last-Event-ID for SSE resumption). seq is assigned by the
SSE Coordinator — workers do not set it.
"""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from trendstorm.shared.ids import new_id


class StreamEventType(StrEnum):
    """All stream event types a client may receive on the SSE channel."""

    # Lifecycle events — one per stage, emitted by each worker
    STAGE_STARTED = "stage_started"
    STAGE_COMPLETED = "stage_completed"
    STAGE_FAILED = "stage_failed"

    # Progress events — emitted periodically by long-running stages
    PROGRESS = "progress"

    # Analyst-specific events
    PARTIAL_TEXT = "partial_text"       # streaming summary token(s)
    CITATION_ADDED = "citation_added"   # one Insight resolved

    # Terminal events — SSE endpoint closes stream on these
    REPORT_READY = "report_ready"       # Publisher finished, Report persisted
    JOB_FAILED = "job_failed"           # unrecoverable failure
    JOB_REJECTED = "job_rejected"       # HITL reviewer declined the analysis

    # HITL review events (Phase 13.5)
    REVIEW_REQUIRED = "review_required"  # analysis paused, awaiting human decision
    REVIEW_RESOLVED = "review_resolved"  # reviewer (or sweeper) made a decision

    # Infrastructure
    HEARTBEAT = "heartbeat"             # SSE comment — client never sees this as data

    @property
    def is_terminal(self) -> bool:
        """True for event types that signal end-of-stream."""
        return self in {
            StreamEventType.REPORT_READY,
            StreamEventType.JOB_FAILED,
            StreamEventType.JOB_REJECTED,
        }


class StreamEvent(BaseModel):
    """A single event destined for a job's SSE stream.

    Fields:
        event_id    — ULID, globally unique (dedup key for SSE coordinator)
        job_id      — partitions the stream; used by SSE endpoint to filter
        tenant_id   — security: SSE endpoint verifies caller owns this job
        event_type  — determines how the client renders the event
        seq         — job-scoped monotonic counter assigned by SSE Coordinator;
                      0 until the coordinator stamps it (workers leave it at 0)
        stage       — which pipeline stage emitted this event (optional)
        payload     — event-type-specific data; arbitrary JSON-serialisable dict
        occurred_at — UTC timestamp of the event
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(default_factory=new_id)
    job_id: str
    tenant_id: str
    event_type: StreamEventType
    seq: int = Field(default=0, ge=0)
    stage: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC)
    )

    def with_seq(self, seq: int) -> StreamEvent:
        """Return a copy stamped with the coordinator-assigned seq number."""
        return self.model_copy(update={"seq": seq})
