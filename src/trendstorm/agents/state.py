"""Shared state for the orchestrator LangGraph workflow.

This module is THE most important Pydantic model in the codebase. Every
LangGraph node reads it and returns a partial update. The checkpointer
serializes it after every transition.

Design principles (locked in for the lifetime of this codebase):

    1. THIN STATE, FAT STORE
       State holds REFERENCES (IDs, URIs), never bulk content.
       Example: state.ingestion.raw_documents = [DocumentRef(id="01H...")]
       The actual HTML lives in MinIO; the parsed text lives in Mongo.

    2. JSON-SERIALIZABLE
       LangGraph checkpoints serialize state via Pydantic's model_dump.
       Use only primitives, lists, dicts, and other Pydantic models.
       Datetimes are fine — Pydantic v2 serializes them to ISO strings.

    3. APPEND-ONLY WITHIN A STAGE
       Once a stage produces a value, downstream nodes must not modify it.
       This preserves the audit trail and enables time-travel debugging.

    4. RETRY BUDGET IS STATE
       Retry counters live in the state because state is the only thing that
       survives pod restarts. Without this, an at-least-once Kafka delivery
       could cause infinite retry loops across pod reboots.

    5. OBSERVABILITY CONTEXT IS BUNDLED
       trace_id and correlation_id travel with state so workers can resume
       a workflow and continue the same trace.

Schema versioning:
    `schema_version` is bumped when the state shape changes. Old checkpoints
    in Mongo with older versions trigger an explicit migration path in the
    worker. Without this field, schema evolution is impossible without
    nuking checkpoints.
"""
from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from trendstorm.agents.stages import Stage
from trendstorm.shared.ids import new_id

# ---------------------------------------------------------------------------
# Reference types — small, IDs only. Bulk data lives elsewhere.
# ---------------------------------------------------------------------------

class SourceRef(BaseModel):
    """Reference to a registered Source (user-defined URL/API/feed)."""

    id: str
    type: str       # SourceType value
    label: str      # human-readable for logs/UI


class DocumentRef(BaseModel):
    """Reference to a raw_documents row, with minimal metadata."""

    id: str
    source_id: str
    content_hash: str
    blob_uri: str | None = None     # MinIO URI for raw bytes
    char_count: int = 0


class ChunkRef(BaseModel):
    """Reference to chunks for retrieval; vector ID lives in the vector store."""

    id: str
    document_id: str


class StageError(BaseModel):
    """A captured error from a stage attempt. Append-only."""

    stage: Stage
    code: str
    message: str
    attempt: int = 1
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    context: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Per-stage state sub-models. Each stage owns its own slice.
# ---------------------------------------------------------------------------

class IngestionState(BaseModel):
    """Outputs of the ingestion stage."""

    raw_documents: list[DocumentRef] = Field(default_factory=list)
    failed_source_ids: list[str] = Field(default_factory=list)


class KnowledgeState(BaseModel):
    """Outputs of the embedding stage."""

    chunk_refs: list[ChunkRef] = Field(default_factory=list)
    embedding_model: str | None = None


class RetrievalState(BaseModel):
    """Outputs of the retrieval stage. Cleared if we loop back for refinement."""

    retrieved_chunk_ids: list[str] = Field(default_factory=list)
    query: str | None = None
    refinement_count: int = 0


class AnalysisState(BaseModel):
    """Outputs of the analysis stage."""

    insights_doc_id: str | None = None      # ref into `analyses` collection
    validation_score: float = 0.0
    validation_passed: bool = False


class PublishingState(BaseModel):
    """Outputs of the publishing stage."""

    report_doc_id: str | None = None        # ref into `reports` collection
    report_blob_uri: str | None = None


class ObservabilityContext(BaseModel):
    """Carried with state so resumed workflows continue the same trace."""

    correlation_id: str
    trace_id: str | None = None      # OTel hex trace_id; None until first span
    parent_span_id: str | None = None


# ---------------------------------------------------------------------------
# Retry budget — central to durability semantics
# ---------------------------------------------------------------------------

# Per-stage retry budgets. Tunable per stage because failure modes differ:
# - Ingestion: external sites flaky, allow more retries.
# - Analysis: LLM calls expensive, fewer retries.
# These are defaults; runtime can override via `JobState.retry_budgets`.
DEFAULT_RETRY_BUDGETS: dict[Stage, int] = {
    Stage.INGESTING: 5,
    Stage.EMBEDDING: 3,
    Stage.RETRIEVING: 3,
    Stage.ANALYZING: 2,
    Stage.PUBLISHING: 3,
}

# Max self-correction loops (RETRIEVING <-> ANALYZING). Without a cap, a
# poorly-calibrated validation_score could loop forever.
MAX_REFINEMENT_LOOPS: int = 2


# ---------------------------------------------------------------------------
# Root state model
# ---------------------------------------------------------------------------

class JobState(BaseModel):
    """The complete state passed between LangGraph nodes.

    Naming convention: `*_completed_at` (past tense) means "the stage
    finished and produced its outputs"; "*_started_at" means "we entered
    this stage" (a node began executing).
    """

    model_config = ConfigDict(
        extra="forbid",
        # Validate on assignment so node bugs that produce bad state surface fast.
        validate_assignment=True,
    )

    # --- Schema versioning ----------------------------------------------
    schema_version: int = 2

    # --- Identity --------------------------------------------------------
    job_id: str
    tenant_id: str
    category_id: str

    # --- Stage tracking --------------------------------------------------
    stage: Stage = Stage.PENDING
    # Counters per stage. Used for retry budgets AND idempotency keys.
    attempts: dict[Stage, int] = Field(default_factory=dict)
    retry_budgets: dict[Stage, int] = Field(
        default_factory=lambda: dict(DEFAULT_RETRY_BUDGETS)
    )
    refinement_loops: int = 0

    # --- Sources to process ---------------------------------------------
    sources: list[SourceRef] = Field(default_factory=list)

    # --- Per-stage outputs (references only) ----------------------------
    ingestion: IngestionState = Field(default_factory=IngestionState)
    knowledge: KnowledgeState = Field(default_factory=KnowledgeState)
    retrieval: RetrievalState = Field(default_factory=RetrievalState)
    analysis: AnalysisState = Field(default_factory=AnalysisState)
    publishing: PublishingState = Field(default_factory=PublishingState)

    # --- HITL review state ----------------------------------------------
    # Set to the ReviewRequest.id when the job is paused for human review.
    pending_review_id: str | None = None
    # Reviewer comment from a "request_refinement" decision; fed into the
    # next AnalysisPendingEvent as refinement_notes and cleared after use.
    review_decision_comment: str | None = None
    # Set True after a review resolves so review_gate_node does not re-gate
    # a refinement-loop analysis that already passed human review.
    skip_hitl_gate: bool = False

    # --- Error log ------------------------------------------------------
    errors: list[StageError] = Field(default_factory=list)

    # --- Observability --------------------------------------------------
    observability: ObservabilityContext

    # --- Timestamps -----------------------------------------------------
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # ------------------------------------------------------------------ #
    # Helpers                                                            #
    # ------------------------------------------------------------------ #

    def remaining_budget(self, stage: Stage) -> int:
        """Return the number of remaining attempts allowed for this stage."""
        used = self.attempts.get(stage, 0)
        budget = self.retry_budgets.get(stage, 0)
        return max(0, budget - used)

    def has_budget(self, stage: Stage) -> bool:
        """Return True if at least one more attempt is permitted."""
        return self.remaining_budget(stage) > 0

    def can_refine(self) -> bool:
        """Return True if we can loop back from ANALYZING to RETRIEVING."""
        return self.refinement_loops < MAX_REFINEMENT_LOOPS

    @classmethod
    def initial(
        cls,
        *,
        tenant_id: str,
        category_id: str,
        sources: list[SourceRef],
        correlation_id: str,
    ) -> JobState:
        """Create and return the initial state for a brand-new job."""
        return cls(
            job_id=new_id(),
            tenant_id=tenant_id,
            category_id=category_id,
            sources=sources,
            observability=ObservabilityContext(correlation_id=correlation_id),
        )
