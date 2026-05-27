"""Kafka event schemas.

These Pydantic models are the WIRE CONTRACT between services. Once a topic
is deployed, its schema is FROZEN for that version. Breaking changes require
a new topic version (`.v2`).

What's safe to add to an existing version?
    - Optional fields with defaults.
What's NOT safe?
    - Renaming fields.
    - Changing field types.
    - Making optional fields required.
    - Removing fields (consumers might depend on them).

Every event has a base `EventEnvelope`:
    - schema_version: forward-compat marker.
    - event_id: unique per produce (for tracing).
    - correlation_id: ties to user-facing request.
    - occurred_at: when the event was produced.
    - traceparent: W3C trace context (lets us span across services).
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from trendstorm.shared.ids import new_id

# ---------------------------------------------------------------------------
# Embedded sub-models — self-contained wire types, independent of JobState
# ---------------------------------------------------------------------------

class IngestDocRef(BaseModel):
    """Document reference embedded in IngestCompletedEvent.

    Kept independent of JobState.DocumentRef so the wire format can evolve
    separately from the in-memory agent state.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    source_id: str
    content_hash: str
    blob_uri_raw: str | None = None
    char_count: int = 0


# ---------------------------------------------------------------------------
# Base envelope — present on every event
# ---------------------------------------------------------------------------

class EventEnvelope(BaseModel):
    """Shared envelope fields. Every concrete event extends this."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    event_id: str = Field(default_factory=new_id)
    correlation_id: str
    tenant_id: str
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # W3C traceparent header (https://www.w3.org/TR/trace-context/).
    # Lets the consumer continue the OTel trace started by the producer.
    traceparent: str | None = None


# ---------------------------------------------------------------------------
# Concrete events — one per topic
# ---------------------------------------------------------------------------

class JobRequestedEvent(EventEnvelope):
    """Topic: trendstorm.jobs.requested.v1.

    Emitted by the API when a user creates a job. The orchestrator consumes
    this and starts the LangGraph workflow.
    """

    event_type: Literal["job.requested"] = "job.requested"
    job_id: str
    category_id: str
    source_ids: list[str]
    note: str | None = None


class IngestPendingEvent(EventEnvelope):
    """Topic: trendstorm.ingest.pending.v1.

    Published once per job by the orchestrator's ingest_node. The scout worker
    processes ALL sources for the job in a single handler invocation, keyed
    by job_id (ScoutWorker._idempotency_key returns f"scout:{job_id}").
    """

    event_type: Literal["ingest.pending"] = "ingest.pending"
    job_id: str
    source_ids: list[str]   # all source IDs for this job
    attempt: int = 1


class IngestCompletedEvent(EventEnvelope):
    """Topic: trendstorm.ingest.completed.v1.

    Published by the scout worker when ingestion for a job finishes.
    Partial success is valid: document_refs carries what succeeded,
    failed_source_ids what did not. The orchestrator resumes the graph
    from the checkpoint and updates JobState.ingestion with these refs.
    """

    event_type: Literal["ingest.completed"] = "ingest.completed"
    job_id: str
    document_refs: list[IngestDocRef] = Field(default_factory=list)
    failed_source_ids: list[str] = Field(default_factory=list)
    # Set only when the entire job fails catastrophically (not partial failure).
    error_code: str | None = None
    error_message: str | None = None


class KnowledgeDocRef(BaseModel):
    """Document to be chunked and embedded — payload of KnowledgePendingEvent."""

    model_config = ConfigDict(extra="forbid")

    document_id: str
    blob_uri_text: str   # s3:// URI to the extracted plain-text artifact
    category_id: str
    source_id: str


class KnowledgeDocResult(BaseModel):
    """Per-document outcome — embedded in KnowledgeCompletedEvent."""

    model_config = ConfigDict(extra="forbid")

    document_id: str
    n_chunks: int = 0
    n_vectors: int = 0
    skipped: bool = False   # True if idempotency hit (already chunked)


class KnowledgePendingEvent(EventEnvelope):
    """Topic: trendstorm.knowledge.pending.v1.

    Published by the orchestrator's embed_node. The knowledge worker processes
    ALL document_refs in a single bounded-concurrency run, keyed by job_id
    (KnowledgeWorker._idempotency_key returns f"knowledge:{job_id}").
    """

    event_type: Literal["knowledge.pending"] = "knowledge.pending"
    job_id: str
    document_refs: list[KnowledgeDocRef]
    attempt: int = 1


class KnowledgeCompletedEvent(EventEnvelope):
    """Topic: trendstorm.knowledge.completed.v1.

    Published by the knowledge worker when chunking + embedding finishes.
    Partial success is valid: document_results carries per-doc outcomes,
    failed_document_ids carries what could not be processed. The orchestrator
    resumes the graph and updates JobState.knowledge.
    """

    event_type: Literal["knowledge.completed"] = "knowledge.completed"
    job_id: str
    document_results: list[KnowledgeDocResult] = Field(default_factory=list)
    failed_document_ids: list[str] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None


class AnalysisPendingEvent(EventEnvelope):
    """Topic: trendstorm.analysis.pending.v1.

    Published by the orchestrator to trigger an Analyst pass. One event per
    refinement loop iteration — each is a distinct work item (per-event
    idempotency key includes refinement_loop so retries do not collapse).

    Carries category_id (the Analyst needs it for retrieval scoping and prompt
    construction) and refinement_notes (validator feedback from the previous
    loop, fed into the next retrieval query).
    """

    event_type: Literal["analysis.pending"] = "analysis.pending"
    job_id: str
    category_id: str
    refinement_loop: int = 0
    refinement_notes: str | None = None
    attempt: int = 1


class AnalysisCompletedEvent(EventEnvelope):
    """Topic: trendstorm.analysis.completed.v1.

    Published by the analyst worker. The orchestrator inspects passed + score
    + refinement_loop against AnalysisSettings.max_refinement_loops to decide
    whether to publish or refine.

    passed is the canonical pass signal (score >= validator_threshold).
    score is the validator's aggregate rubric score (0.0-1.0).
    On catastrophic failure (LLM unreachable, schema parse failure, etc.),
    set success=False and provide error_code/error_message; analysis_id will be None.
    """

    event_type: Literal["analysis.completed"] = "analysis.completed"
    job_id: str
    success: bool
    analysis_id: str | None = None
    passed: bool = False
    score: float = 0.0
    refinement_loop: int = 0
    error_code: str | None = None
    error_message: str | None = None


class PublishPendingEvent(EventEnvelope):
    event_type: Literal["publish.pending"] = "publish.pending"
    job_id: str
    analysis_id: str
    category_id: str
    attempt: int = 1


class PublishCompletedEvent(EventEnvelope):
    """Topic: trendstorm.publish.completed.v1.

    Published by the publisher worker once all report formats are rendered
    and persisted. The orchestrator advances the job to COMPLETED on receipt.

    markdown_report_id is always set (PDF may be None if weasyprint failed).
    On catastrophic failure, success=False; report IDs are all None.
    """

    event_type: Literal["publish.completed"] = "publish.completed"
    job_id: str
    success: bool
    markdown_report_id: str | None = None
    pdf_report_id: str | None = None
    json_report_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class EvalSampleEvent(EventEnvelope):
    """Topic: trendstorm.eval.sample.v1.

    Published by the analyst worker for analyses chosen for production sampling
    (hash(job_id) % 100 == 0 — deterministic 1% rate). The production-eval
    worker consumes this, runs the faithfulness + citation + relevance evaluators,
    and persists EvaluationResult to Mongo. Low-scoring results are flagged for
    golden dataset curation.

    sampled_at is the wall-clock time the analyst decided to sample this analysis
    (not when the eval worker processes it — the lag may be seconds or minutes).
    """

    event_type: Literal["eval.sample"] = "eval.sample"
    job_id: str
    analysis_id: str
    sampled_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class StreamPartialEvent(EventEnvelope):
    """Carries a single StreamEvent through Kafka to the SSE coordinator.

    Workers publish this; the SSE coordinator:
        1. Assigns job-scoped seq via Redis INCR.
        2. Stamps the payload with seq and writes to Redis Streams (XADD).
        3. Publishes to Redis Pub/Sub for live SSE fanout.

    The EventEnvelope.event_id serves as the SSE coordinator idempotency
    key (f"sse:{event.event_id}") — duplicate Kafka deliveries must not
    corrupt the monotonic seq counter.
    """

    event_type: Literal["stream.partial"] = "stream.partial"
    job_id: str
    stream_event_type: str          # StreamEventType value (e.g. "stage_started")
    stage: str | None = None        # pipeline stage emitting this event
    stream_payload: dict[str, object] = Field(   # event-type-specific data
        default_factory=dict
    )


class ReviewRequestedEvent(EventEnvelope):
    """Topic: trendstorm.review.requested.v1.

    Published by review_gate_node when a job's analysis is held for human
    review. The SSE coordinator forwards this to the tenant's live stream as
    a review_required event. Reviewers poll GET /v1/reviews?status=pending
    or rely on SSE to know a review is waiting.
    """

    event_type: Literal["review.requested"] = "review.requested"
    job_id: str
    review_id: str
    analysis_id: str
    validator_score: float
    refinement_loops: int
    cost_so_far_usd: float = 0.0
    timeout_at: datetime


class ReviewResolvedEvent(EventEnvelope):
    """Topic: trendstorm.review.resolved.v1.

    Published via outbox by the review API (POST /v1/reviews/{id}/resolve) and
    directly by the timeout sweeper worker. The orchestrator consumes this to
    resume the LangGraph workflow with the reviewer's decision.

    decision: one of "approve" | "reject" | "request_refinement"
    comment: present only when decision=request_refinement; forwarded as the
             next analyst loop's refinement_notes.
    resolved_by: reviewer principal (key_id / JWT subject) or "timeout_sweeper".
    """

    event_type: Literal["review.resolved"] = "review.resolved"
    job_id: str
    review_id: str
    decision: str         # ReviewDecision value
    comment: str | None = None
    resolved_by: str | None = None


# ---------------------------------------------------------------------------
# Discriminated union — lets a single consumer parse any event type.
# Use:  parsed = TypeAdapter(AnyEvent).validate_json(message_bytes)
# ---------------------------------------------------------------------------

AnyEvent = Annotated[
    JobRequestedEvent
    | IngestPendingEvent | IngestCompletedEvent
    | KnowledgePendingEvent | KnowledgeCompletedEvent
    | AnalysisPendingEvent | AnalysisCompletedEvent
    | PublishPendingEvent | PublishCompletedEvent
    | StreamPartialEvent
    | EvalSampleEvent
    | ReviewRequestedEvent | ReviewResolvedEvent,
    Field(discriminator="event_type"),
]
