"""Vector store domain models."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class VectorHit(BaseModel):
    """Single result from a vector similarity search.

    score is normalised to [0, 1] — higher means more similar. The concrete
    store implementation is responsible for converting raw distances (e.g.
    ChromaDB cosine distance 0-2) to this scale before constructing VectorHit.

    metadata carries the values stored alongside the vector at upsert time.
    Expected keys: tenant_id, category_id, document_id, source_id.
    The retrieve_node (Phase 8) uses category_id to filter cross-category bleed.
    """

    model_config = ConfigDict(extra="forbid")

    id: str  # chunk_id — FK back to Mongo Chunk collection
    score: float = Field(..., ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)
    document: str | None = None  # text, if the store was asked to return it
