"""Job domain model.

The Job is a first-class business entity:
    - Tracks user-facing status of an analysis request.
    - Stores high-level metadata (category, sources, timestamps).
    - References the LangGraph checkpoint (which holds detailed state).
    - References the final outputs (analysis_id, report_id).

This is INTENTIONALLY separate from JobState (the LangGraph runtime state):
    - Job is what we expose in the API and what users care about.
    - JobState is internal orchestrator vocabulary.
    - The two can evolve at different paces; the Job model is part of the
      public contract, while JobState is a private implementation detail.
"""
from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from trendstorm.shared.ids import new_id
from trendstorm.shared.types import JobStatus


class JobMetrics(BaseModel):
    """Aggregated metrics for a job. Updated as stages complete."""

    documents_ingested: int = 0
    chunks_created: int = 0
    chunks_retrieved: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    duration_seconds: float | None = None       # set when terminal


class Job(BaseModel):
    """A trend analysis job.

    Lifecycle:
        - Created by JobService.create_job
        - Status updates emitted by orchestrator nodes
        - Final state (COMPLETED|FAILED|CANCELLED) is terminal
    """

    model_config = ConfigDict(extra="forbid")

    # --- Identity ---
    id: str = Field(default_factory=new_id)
    tenant_id: str
    category_id: str

    # --- Lifecycle ---
    status: JobStatus = JobStatus.PENDING
    source_ids: list[str] = Field(default_factory=list)
    note: str | None = None

    # --- References to outputs (populated as stages complete) ---
    analysis_id: str | None = None
    report_id: str | None = None

    # --- Metrics ---
    metrics: JobMetrics = Field(default_factory=JobMetrics)

    # --- Failure info (only when status == FAILED) ---
    failure_code: str | None = None
    failure_message: str | None = None

    # --- Timestamps ---
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
