"""Shared Pydantic models for the TrendStorm AI wire format.

These mirror the server's API router response schemas. The SDK uses them
as typed return types; the server can import them for schema validation in tests.

All timestamps are datetime objects (UTC-aware). The server serialises them as
ISO-8601 strings; Pydantic parses them back automatically.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from trendstorm_shared.types import (
    FlaggingReason,
    JobStatus,
    ReviewDecision,
    ReviewStatus,
    SourceType,
    StreamEventType,
)


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


class CategoryResponse(_Base):
    id: str
    name: str
    description: str | None = None
    keywords: list[str] = Field(default_factory=list)
    archived: bool = False
    created_at: datetime
    updated_at: datetime


class CategoryListResponse(_Base):
    categories: list[CategoryResponse]
    next_cursor: str | None = None


class CreateCategoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    keywords: list[str] = Field(default_factory=list)


class UpdateCategoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str | None = None
    keywords: list[str] | None = None
    archived: bool | None = None


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


class SourceResponse(_Base):
    id: str
    category_id: str
    url: str
    label: str | None = None
    type: SourceType = SourceType.HTTP
    enabled: bool = True
    last_fetch_at: datetime | None = None
    last_fetch_status: str | None = None
    last_fetch_error: str | None = None
    created_at: datetime


class SourceListResponse(_Base):
    sources: list[SourceResponse]


class RegisterSourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_id: str = Field(..., min_length=26, max_length=26)
    url: str = Field(..., min_length=4, max_length=4096)
    label: str | None = Field(default=None, max_length=200)
    type: SourceType = SourceType.HTTP


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


class JobMetricsResponse(_Base):
    documents_ingested: int = 0
    chunks_created: int = 0
    chunks_retrieved: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    duration_seconds: float | None = None


class JobResponse(_Base):
    id: str
    status: JobStatus
    category_id: str
    source_ids: list[str] = Field(default_factory=list)
    note: str | None = None
    analysis_id: str | None = None
    report_id: str | None = None
    metrics: JobMetricsResponse = Field(default_factory=JobMetricsResponse)
    failure_code: str | None = None
    failure_message: str | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    stream_url: str | None = None


class JobAcceptedResponse(_Base):
    job_id: str
    status: JobStatus
    stream_url: str
    created_at: datetime


class JobListResponse(_Base):
    jobs: list[JobResponse]
    next_cursor: str | None = None


class CreateJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_id: str = Field(..., description="ID of an existing trend category")
    source_ids: list[str] = Field(default_factory=list)
    note: str | None = Field(default=None, max_length=500)


# ---------------------------------------------------------------------------
# Reviews (HITL)
# ---------------------------------------------------------------------------


class ReviewResponse(_Base):
    id: str
    job_id: str
    analysis_id: str
    stage_under_review: str
    status: ReviewStatus
    reviewer_id: str | None = None
    decision_comment: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None
    timeout_at: datetime
    sla_seconds: int
    # Fields added in Phase 15.6 — populated by review_gate_node
    validator_score: float | None = None
    refinement_loops_used: int = 0
    cost_usd_so_far_cents: int = 0
    flagging_reason: FlaggingReason | None = None


class ResolveReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: ReviewDecision
    comment: str | None = Field(
        default=None,
        max_length=2000,
        description="Required when decision=request_refinement.",
    )


# ---------------------------------------------------------------------------
# Quota / usage
# ---------------------------------------------------------------------------


class QuotaResponse(_Base):
    allowed: bool
    monthly_spend_usd: float
    monthly_limit_usd: float
    jobs_this_month: int
    jobs_limit: int
    reason: str | None = None


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------


class ApiKeyCreatedResponse(_Base):
    id: str
    name: str
    key: str
    key_prefix: str
    tenant_id: str
    created_at: datetime


class ApiKeyResponse(_Base):
    id: str
    name: str
    key_prefix: str
    tenant_id: str
    created_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    is_active: bool = True


class ApiKeyListResponse(_Base):
    keys: list[ApiKeyResponse]


class CreateApiKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=100)


# ---------------------------------------------------------------------------
# Memories (Phase 15.5+)
# ---------------------------------------------------------------------------


class CreateMemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=4000)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    tags: list[str] = Field(default_factory=list)
    curated_by: str = Field(min_length=1, max_length=256)


class MemoryResponse(_Base):
    id: str
    tenant_id: str
    category_id: str
    kind: str
    source: str
    content: str
    confidence: float
    is_active: bool
    tags: list[str]
    superseded_by: str | None = None
    created_at: datetime
    updated_at: datetime


class MemoryListResponse(_Base):
    items: list[MemoryResponse]
    total: int


# ---------------------------------------------------------------------------
# Analyses (Phase 15.6 — exposed via GET /v1/jobs/{id}/analysis)
# ---------------------------------------------------------------------------


class AnalysisResponse(_Base):
    id: str
    job_id: str
    summary: str
    validator_score: float
    validator_passed: bool
    refinement_loops: int = 0
    created_at: datetime


# ---------------------------------------------------------------------------
# Audit log (Phase 15.6 — exposed via GET /v1/audit)
# ---------------------------------------------------------------------------


class AuditLogEntryResponse(_Base):
    id: str
    tenant_id: str
    event_type: str
    actor: str
    resource_type: str
    resource_id: str
    action: str
    outcome: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    trace_id: str | None = None
    correlation_id: str | None = None


class AuditLogListResponse(_Base):
    items: list[AuditLogEntryResponse]
    next_cursor: str | None = None


# ---------------------------------------------------------------------------
# Streaming events
# ---------------------------------------------------------------------------


class StreamEvent(_Base):
    event_id: str
    job_id: str
    tenant_id: str
    event_type: StreamEventType
    seq: int = 0
    stage: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime
