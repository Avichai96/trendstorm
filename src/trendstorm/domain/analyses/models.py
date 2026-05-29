"""Analysis domain model.

The structured output of the Analyst agent (Phase 8/9). Stored in Mongo
because it's THE source of truth for downstream consumers (the Publisher
agent, the API, future evaluation jobs).

Why not store in MinIO like raw HTML?
    - Queryable: we want "show me all analyses for category X" without
      downloading blobs. Mongo gives us indexes.
    - Smaller: typically 5-50KB, well within Mongo's sweet spot.
    - Mutable enough: validation may rerun and overwrite the analysis
      object. MinIO would need versioning gymnastics.

Why not embed in the Job?
    - Jobs are listed in dashboards; we don't want to ship 50KB of
      analysis content for every job in a list view.
    - Analyses outlive their jobs (jobs TTL at 90d, analyses at 1y).
    - Analyses can be inspected independently for evaluation.

Schema design:
    `insights` is a structured list. Each insight has:
      - claim:      the trend assertion ("LLM safety is becoming policy")
      - evidence:   list of chunk_ids that support the claim
      - confidence: model's self-rated confidence
      - tags:       category-defined or auto-generated

    `citations` is a parallel list of (chunk_id -> excerpt) so we can
    render citations without re-fetching every chunk's text.

    `validator_score` and `validator_notes` come from the validation
    pass (whether the analyst's own work passes the rubric).
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from trendstorm.shared.ids import new_id


class Insight(BaseModel):
    """One structured assertion within an analysis."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    claim: str = Field(..., min_length=1, max_length=2000)
    rationale: str | None = Field(default=None, max_length=5000)
    # IDs of chunks that support this claim. Used for "show source" UX.
    supporting_chunk_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(..., ge=0.0, le=1.0)
    tags: list[str] = Field(default_factory=list)


class Citation(BaseModel):
    """A short excerpt for in-report referencing."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    document_id: str
    source_id: str
    excerpt: str = Field(..., max_length=500)
    url: str | None = None  # convenience: original URL of the source


class Analysis(BaseModel):
    """The Analyst agent's structured output."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    tenant_id: str
    job_id: str
    category_id: str

    # The high-level executive summary.
    summary: str = Field(..., min_length=1, max_length=10000)

    # Structured findings.
    insights: list[Insight] = Field(default_factory=list)

    # Cited evidence.
    citations: list[Citation] = Field(default_factory=list)

    # Validator outputs (from the validation pass; see Phase 4 refinement loop).
    validator_score: float = Field(default=0.0, ge=0.0, le=1.0)
    validator_passed: bool = False
    validator_notes: str | None = Field(default=None, max_length=5000)

    # Model provenance — critical for evaluation and debugging.
    model_name: str | None = None
    model_provider: str | None = None  # "anthropic" | "openai" | "ollama"
    input_tokens: int = 0
    output_tokens: int = 0

    # How many refinement loops this analysis went through.
    refinement_loops: int = 0

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
