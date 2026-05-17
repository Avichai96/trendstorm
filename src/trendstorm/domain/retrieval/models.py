"""Retrieval domain models.

RetrievalRequest carries the query + tenant/category filters.
RetrievedChunk is what every stage of the retrieval pipeline produces and
consumes: a ranked result with the text needed for the LLM and the provenance
needed to build Citation objects in the Analysis.

Score semantics:
    - After BM25 or vector retrieval: backend-native (not normalised).
    - After RRF merge: RRF score (higher = better; range depends on k and list count).
    - After cross-encoder reranking: reranker relevance score (0-1, Cohere convention).
    Downstream code must not assume a particular range; only relative ordering matters.

Parent text:
    Child chunks are embedded and retrieved; parent chunks supply wider context to
    the LLM. parent_text is None when the retrieved chunk IS a parent (no further
    expansion needed) or when parent lookup failed (treated as best-effort).
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RetrievalRequest(BaseModel):
    """Parameters for a single retrieval call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str = Field(..., min_length=1, max_length=2000)
    tenant_id: str
    category_id: str
    top_k: int = Field(default=10, ge=1, le=500)


class RetrievedChunk(BaseModel):
    """One ranked retrieval result, ready for the LLM prompt.

    text       — child chunk text (short; used for reranking and embedding).
    parent_text — wider context for the LLM; None if the chunk is already a
                  parent or if parent fetch failed.
    document_id / source_id / source_url — provenance fields consumed by the
                  Analyst to build Citation objects without extra DB round-trips.
    """

    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    score: float = Field(..., description="Relative ranking score; higher is better.")
    text: str = Field(..., min_length=1)
    parent_text: str | None = None

    # Provenance — mirrors Citation fields so the Analyst can build them directly.
    document_id: str
    source_id: str
    source_url: str | None = None
