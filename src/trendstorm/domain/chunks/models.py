"""Chunk domain model.

A Chunk is one retrieval-sized slice of a RawDocument. It stores the
SHORT TEXT of the chunk for BM25 + reranker, plus pointers into the
vector store. The dense vector itself lives in ChromaDB.

Architectural decision: why split chunk text from chunk vector?

Mongo stores:
    - `text` (the actual chunk content, < 5KB)
    - `position` (chunk index within doc, for ordering)
    - `parent/child relations` (for the parent-doc retrieval pattern)
    - `vector_id` (FK into Chroma)

ChromaDB stores:
    - The embedding vector
    - A copy of metadata for filtering at retrieval time
    - The `chunk_id` (FK back into Mongo)

The duplication of `chunk_id` is intentional: a vector search returns
top-K vector_ids; we then need their text for the reranker and for the
LLM. We could put text in Chroma too, but then a chunk update means
two writes; keeping text in Mongo makes Mongo the source of truth.

Parent-child chunking:
    Phase 7 will implement parent-child chunking: SHORT chunks (e.g. 200
    tokens) get embedded and retrieved, but the LLM is given the LARGER
    parent chunk (e.g. 1000 tokens) as context. Both rows live here;
    `parent_chunk_id` links a child to its parent. Parents have
    `parent_chunk_id = None`.

Why not just one big chunk size?
    Embedding quality is best with short, semantically focused chunks
    (200 tokens). LLM context quality is best with longer, narratively
    complete chunks (1000+ tokens). Parent-child gets you both.
"""
from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from trendstorm.shared.ids import new_id


class Chunk(BaseModel):
    """One retrieval unit derived from a RawDocument."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    tenant_id: str
    job_id: str            # which job created this chunk (for cleanup + auditing)
    category_id: str       # denormalized for the vector store's metadata filter
    document_id: str       # parent RawDocument
    source_id: str         # denormalized; saves a join when displaying provenance

    # Position within the document, 0-indexed. Used for stitching context.
    position: int = Field(..., ge=0)

    # The chunk text. Kept under 8KB to play nice with index entries.
    text: str = Field(..., min_length=1, max_length=8192)

    # Token count under the embedding model's tokenizer.
    token_count: int = 0

    # Pointer into the vector store. None means "embedding pending."
    vector_id: str | None = None
    # Which embedding model was used (e.g. "text-embedding-3-small", "nomic-embed-text").
    # Critical for re-embedding when we swap models.
    embedding_model: str | None = None

    # Parent-child chunking. If parent_chunk_id is None, this IS a parent.
    parent_chunk_id: str | None = None

    # Char offset within the parent doc — for source citation in reports.
    char_start: int | None = None
    char_end: int | None = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime | None = Field(default=None)
