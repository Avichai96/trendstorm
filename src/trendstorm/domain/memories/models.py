"""Long-term memory domain models (Phase 15.5).

Two memory kinds are produced per completed job:
    - Episodic: one record per job — a summary of what happened (job outcome).
      Automatic, no HITL gate. Source of truth for "what did we analyse before."
    - Semantic: N factual claims extracted from the analysis by a lightweight LLM
      pass. Gated by HITL approval when hitl_mode != off.

Supersede lifecycle:
    When the semantic extractor detects that a new claim contradicts an existing
    active memory (cosine similarity > threshold AND opposite-sentiment check),
    it marks the old memory's `superseded_by` to the new memory's id. Superseded
    memories are excluded from retrieval but retained for audit.

User-curated memories have `source = "user_curated"` and carry `curated_by`
(the API key or JWT sub of the creator). They are created via POST
/v1/categories/{id}/memories with `tenant_admin` role.
"""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from trendstorm.shared.ids import new_id

_TTL_YEARS_2 = 730  # days; used in index definition only, not enforced here


class MemoryKind(StrEnum):
    SEMANTIC = "semantic"
    EPISODIC = "episodic"


class MemorySource(StrEnum):
    JOB_OUTCOME = "job_outcome"        # episodic — written by publisher post-job
    EXTRACTED = "extracted"            # semantic — extracted by LLM from analysis
    USER_CURATED = "user_curated"      # written via API by tenant admin


class Memory(BaseModel):
    """A single unit of long-term memory scoped to a tenant + category.

    Retrieval note: `content` is the plain-text claim or summary that is
    embedded and searched. Chunk text is NOT stored here — chunks expire
    and rotate; memories persist.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    tenant_id: str
    category_id: str

    kind: MemoryKind
    source: MemorySource

    content: str                    # the durable claim / episodic summary
    confidence: float = Field(ge=0.0, le=1.0)

    # Provenance — tracing back to the job that produced this memory
    source_job_id: str
    source_analysis_id: str

    # ChromaDB vector reference
    content_embedding_id: str | None = None   # set after Chroma upsert

    # Supersession — set when a newer claim contradicts this one
    superseded_by: str | None = None          # Memory.id of the superseding record
    is_active: bool = True                    # False when superseded or user-deleted

    tags: list[str] = Field(default_factory=list)

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class UserCuratedMemory(Memory):
    """Memory created explicitly by a tenant admin via the API.

    Extends Memory with:
        - source is always USER_CURATED
        - curated_by carries the reviewer principal (API key ID or JWT sub)
    """

    model_config = ConfigDict(extra="forbid")

    source: Literal[MemorySource.USER_CURATED] = MemorySource.USER_CURATED
    curated_by: str              # API key id or JWT subject of creator
