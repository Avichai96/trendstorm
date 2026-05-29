"""RawDocument domain model.

The METADATA of an ingested piece of content. The actual content (HTML,
parsed text) lives in MinIO. This model stores just enough to:
    - Look up where the bytes live (`blob_uri`).
    - Decide whether to re-fetch (cache via `content_hash`).
    - Track ingestion provenance (which source, which job).
    - Show the user a list of "what was used in this analysis."

We deliberately do NOT store the parsed text in Mongo. Reasons:

1. **Size.** A scraped article averages 5-50KB; multiply by 100 sources
   per job and you're at ~5MB per job in Mongo. With 1000 jobs/day that's
   5GB/day of mostly-cold-after-ingestion data. MinIO costs ~10x less
   per GB and supports zero-copy serving.

2. **Lifecycle mismatch.** Raw text is written ONCE during ingestion,
   read maybe TWICE during the analysis run, and then rarely. Mongo
   indexes that data forever and pays per-doc storage tax. MinIO charges
   for storage, not for indexing.

3. **Query pattern.** No query ever does `WHERE raw_text LIKE ...` on
   this collection — that's what the vector store and BM25 index on
   `chunks` are for. Mongo is the wrong tool for full-text on raw docs.

What we DO store:
    - `content_hash`: SHA-256 of the extracted text. Used for dedup
      across sources (two sources scraping the same article -> one doc).
    - `extracted_at`: when we ran the parser. Lets us re-parse with newer
      extraction logic without re-fetching.
    - `char_count`, `word_count`: for chunking budgets and UI displays.
    - `fetch_metadata`: HTTP status, redirect chain, content-type — for
      debugging "why is this source failing?"
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from trendstorm.shared.ids import new_id


class FetchMetadata(BaseModel):
    """HTTP-level debugging info from the fetch. Tolerant of missing fields."""

    model_config = ConfigDict(extra="ignore")

    http_status: int | None = None
    content_type: str | None = None
    bytes_fetched: int = 0
    final_url: str | None = None  # after redirects
    fetch_duration_ms: int | None = None


class RawDocument(BaseModel):
    """Metadata for an ingested piece of content."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    tenant_id: str
    job_id: str
    category_id: str  # denormalized for "find all docs in this category" queries
    source_id: str

    # The original URL (after canonicalization) we fetched.
    url: str = Field(..., max_length=4096)
    # Hash of the EXTRACTED TEXT, not the raw HTML. Two pages with
    # identical text but different ads / nav should dedup.
    content_hash: str

    # Sizes — used for chunk budgeting and UI.
    char_count: int = 0
    word_count: int = 0

    # Where the bytes live. MinIO URI of the form
    # "s3://trendstorm-raw/{tenant}/{job_id}/{doc_id}/raw.html"
    blob_uri_raw: str | None = None
    blob_uri_text: str | None = None  # parsed-text artifact

    # Detected language (langdetect or similar). Optional — not all pages have one.
    language: str | None = None

    # Title extracted by trafilatura/equivalent. Helpful in the UI.
    title: str | None = Field(default=None, max_length=500)

    fetch_metadata: FetchMetadata = Field(default_factory=FetchMetadata)

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # Set when the parser finished. Distinct from created_at because
    # fetch and parse can be different runs (re-parse without re-fetch).
    extracted_at: datetime | None = None
