"""All MongoDB indexes for TrendStorm, in one place.

Every index this codebase relies on is declared HERE. The seeder script
(`scripts/seed_mongo_indexes.py`) reads this module and applies them.
Tests that depend on a specific index reference these definitions so they
break loudly when the index is renamed or removed.

Index design principles applied throughout:

1. **Tenant first.**
   Every multi-tenant collection's primary list index starts with
   `(tenant_id, ...)`. This ensures one tenant's reads never compete with
   another's at the index level.

2. **Compound > multiple single-field.**
   `(a, b, c)` serves queries on `(a)`, `(a, b)`, `(a, b, c)` — three for
   the price of one. Mongo's leftmost-prefix rule.

3. **Sort matches the index direction.**
   If you `.sort({_id: -1})` you need `_id: -1` (or no direction marker)
   in the index. The wrong direction forces an in-memory sort.

4. **TTL goes on its own dedicated field.**
   Mongo TTL indexes can ONLY be single-field. Don't fold TTL into a
   compound index — it won't expire anything.

5. **Partial indexes for skewed status fields.**
   If 95% of jobs are COMPLETED, indexing all of them wastes RAM. Partial
   indexes on (status != completed) are tiny and serve the hot
   "find in-progress jobs" query without bloat.

6. **Unique indexes are constraints, not optimizations.**
   They prevent duplicate-key writes. `idempotency._id` is unique. Don't
   confuse with non-unique indexes used for query speed.

Maintaining indexes:
    - To ADD an index: append to this list. Re-run the seeder. Idempotent.
    - To CHANGE an index: this is a multi-step operation in prod (build new
      index in background, switch queries to it, drop old). Don't shortcut.
    - To DROP an index: remove from this list AND delete it in Mongo. The
      seeder doesn't auto-drop (too dangerous).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pymongo import ASCENDING, DESCENDING, TEXT

from trendstorm.infrastructure.mongo.schema import Collection
from trendstorm.shared.types import JobStatus

# ---------------------------------------------------------------------------
# Common time constants
# ---------------------------------------------------------------------------

_SECONDS_PER_DAY = 24 * 3600
_TTL_90_DAYS = 90 * _SECONDS_PER_DAY
_TTL_365_DAYS = 365 * _SECONDS_PER_DAY


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IndexSpec:
    """Declarative index definition.

    Maps 1:1 onto pymongo's `create_index(keys, **options)`. We use a
    dataclass so tests and the seeder can iterate without re-parsing strings.
    """

    collection: Collection
    keys: list[tuple[str, int]]
    name: str
    unique: bool = False
    sparse: bool = False
    expire_after_seconds: int | None = None
    partial_filter_expression: dict[str, Any] | None = None
    background: bool = True  # production index builds are always background

    def to_pymongo_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"name": self.name, "background": self.background}
        if self.unique:
            kwargs["unique"] = True
        if self.sparse:
            kwargs["sparse"] = True
        if self.expire_after_seconds is not None:
            kwargs["expireAfterSeconds"] = self.expire_after_seconds
        if self.partial_filter_expression is not None:
            kwargs["partialFilterExpression"] = self.partial_filter_expression
        return kwargs


# ---------------------------------------------------------------------------
# Index definitions per collection
# ---------------------------------------------------------------------------

INDEXES: list[IndexSpec] = [
    # =======================================================================
    # tenants — one row per customer organisation
    # =======================================================================
    # Unique: tenant name must be globally unique (not per-tenant scoped —
    # tenants are the root of the hierarchy).
    IndexSpec(
        collection=Collection.TENANTS,
        keys=[("name", ASCENDING)],
        name="tenants__name_unique",
        unique=True,
    ),
    # =======================================================================
    # api_keys — hashed bearer credentials
    # =======================================================================
    # Primary request-path lookup: "which tenant owns this key?"
    # Must be unique — two tenants cannot collide on the same hash.
    IndexSpec(
        collection=Collection.API_KEYS,
        keys=[("key_hash", ASCENDING)],
        name="api_keys__key_hash_unique",
        unique=True,
    ),
    # Tenant management: "list all keys for this tenant."
    IndexSpec(
        collection=Collection.API_KEYS,
        keys=[("tenant_id", ASCENDING), ("created_at", DESCENDING)],
        name="api_keys__tenant_created",
    ),
    # Active-keys partial index (excludes revoked rows — they're tombstones).
    IndexSpec(
        collection=Collection.API_KEYS,
        keys=[("tenant_id", ASCENDING), ("key_hash", ASCENDING)],
        name="api_keys__tenant_hash_active",
        partial_filter_expression={"revoked_at": None},
    ),
    # =======================================================================
    # categories — user-curated trend topics
    # =======================================================================
    # Query: "list this tenant's categories, newest first."
    IndexSpec(
        collection=Collection.CATEGORIES,
        keys=[("tenant_id", ASCENDING), ("_id", DESCENDING)],
        name="categories__tenant_id",
    ),
    # Query: "find a category by name within a tenant."
    # Used by upsert flows and dedup checks.
    IndexSpec(
        collection=Collection.CATEGORIES,
        keys=[("tenant_id", ASCENDING), ("name", ASCENDING)],
        name="categories__tenant_name_unique",
        unique=True,
    ),
    # =======================================================================
    # sources — URLs/feeds registered under categories
    # =======================================================================
    # Query: "list sources for a category."
    IndexSpec(
        collection=Collection.SOURCES,
        keys=[
            ("tenant_id", ASCENDING),
            ("category_id", ASCENDING),
            ("_id", DESCENDING),
        ],
        name="sources__tenant_category",
    ),
    # Query: "find by URL (dedup at registration time)."
    # Composite with tenant_id because the same URL can legally exist for
    # multiple tenants — Wikipedia is in everyone's source list.
    IndexSpec(
        collection=Collection.SOURCES,
        keys=[("tenant_id", ASCENDING), ("url_hash", ASCENDING)],
        name="sources__tenant_url_hash_unique",
        unique=True,
    ),
    # =======================================================================
    # jobs — execution metadata (introduced Phase 4; expanded here)
    # =======================================================================
    # Query: "list this tenant's jobs, newest first, optionally filtered by status."
    # Serves the dashboard's main list view.
    IndexSpec(
        collection=Collection.JOBS,
        keys=[
            ("tenant_id", ASCENDING),
            ("status", ASCENDING),
            ("created_at", DESCENDING),
        ],
        name="jobs__tenant_status_created",
    ),
    # Query: "find stuck jobs across all tenants" — operator dashboard.
    # Partial index: only non-terminal statuses. Saves ~95% of the
    # index size on a healthy system where most jobs are completed.
    IndexSpec(
        collection=Collection.JOBS,
        keys=[("status", ASCENDING), ("updated_at", ASCENDING)],
        name="jobs__status_updated_in_progress",
        partial_filter_expression={
            "status": {"$in": [s.value for s in JobStatus if not s.is_terminal]}
        },
    ),
    # Query: "show me jobs in this category, terminal or not."
    IndexSpec(
        collection=Collection.JOBS,
        keys=[
            ("tenant_id", ASCENDING),
            ("category_id", ASCENDING),
            ("created_at", DESCENDING),
        ],
        name="jobs__tenant_category_created",
    ),
    # TTL: auto-delete jobs after 90 days.
    IndexSpec(
        collection=Collection.JOBS,
        keys=[("created_at", ASCENDING)],
        name="jobs__ttl_created",
        expire_after_seconds=_TTL_90_DAYS,
    ),
    # =======================================================================
    # idempotency — at-least-once safety net
    # =======================================================================
    # _id is unique by definition; only need the TTL.
    IndexSpec(
        collection=Collection.IDEMPOTENCY,
        keys=[("expires_at", ASCENDING)],
        name="idempotency__ttl",
        # expireAfterSeconds=0 means "use the value in `expires_at` as the
        # absolute expiration time." Each doc is responsible for its own
        # TTL deadline.
        expire_after_seconds=0,
    ),
    # =======================================================================
    # raw_documents — ingested content metadata (real text in MinIO)
    # =======================================================================
    # Query: "list documents fetched by this job, in order."
    IndexSpec(
        collection=Collection.RAW_DOCUMENTS,
        keys=[
            ("tenant_id", ASCENDING),
            ("job_id", ASCENDING),
            ("_id", ASCENDING),
        ],
        name="raw_documents__tenant_job",
    ),
    # Query: "is this URL already ingested?" — dedup at ingest time.
    # Hash of (url + canonical_form) to avoid storing long URLs in the key.
    IndexSpec(
        collection=Collection.RAW_DOCUMENTS,
        keys=[
            ("tenant_id", ASCENDING),
            ("content_hash", ASCENDING),
        ],
        name="raw_documents__tenant_content_hash",
    ),
    # TTL: 1 year from ingestion.
    IndexSpec(
        collection=Collection.RAW_DOCUMENTS,
        keys=[("created_at", ASCENDING)],
        name="raw_documents__ttl_created",
        expire_after_seconds=_TTL_365_DAYS,
    ),
    # =======================================================================
    # chunks — retrieval metadata
    # =======================================================================
    # Query: "get all chunks for a document" (rare; mostly for debugging).
    IndexSpec(
        collection=Collection.CHUNKS,
        keys=[
            ("tenant_id", ASCENDING),
            ("document_id", ASCENDING),
            ("position", ASCENDING),
        ],
        name="chunks__tenant_document_position",
    ),
    # Text index for BM25 fallback (Phase 8 hybrid search).
    # Mongo's text index is sufficient for BM25-style scoring. We rely
    # on it being available; the chunker writes a `text` field that this
    # indexes.
    IndexSpec(
        collection=Collection.CHUNKS,
        keys=[("text", TEXT)],  # type: ignore[list-item]  # TEXT is the marker
        name="chunks__text_bm25",
    ),
    # TTL.
    IndexSpec(
        collection=Collection.CHUNKS,
        keys=[("created_at", ASCENDING)],
        name="chunks__ttl_created",
        expire_after_seconds=_TTL_365_DAYS,
    ),
    # =======================================================================
    # analyses — LLM-generated insights
    # =======================================================================
    # Query: "show this analysis" — by id, scoped by tenant.
    # The list view of analyses goes through `jobs` (jobs.analysis_id).
    # Query: "this tenant's analyses for dashboards / billing reports."
    IndexSpec(
        collection=Collection.ANALYSES,
        keys=[("tenant_id", ASCENDING), ("created_at", DESCENDING)],
        name="analyses__tenant_created",
    ),
    # TTL.
    IndexSpec(
        collection=Collection.ANALYSES,
        keys=[("created_at", ASCENDING)],
        name="analyses__ttl_created",
        expire_after_seconds=_TTL_365_DAYS,
    ),
    # =======================================================================
    # reports — generated reports metadata (blobs in MinIO)
    # =======================================================================
    IndexSpec(
        collection=Collection.REPORTS,
        keys=[("tenant_id", ASCENDING), ("created_at", DESCENDING)],
        name="reports__tenant_created",
    ),
    IndexSpec(
        collection=Collection.REPORTS,
        keys=[("created_at", ASCENDING)],
        name="reports__ttl_created",
        expire_after_seconds=_TTL_365_DAYS,
    ),
    # =======================================================================
    # evaluations — production eval results from the 1% sampling pipeline
    # =======================================================================
    # Primary query: "all eval results for this tenant, most recent first."
    IndexSpec(
        collection=Collection.EVALUATIONS,
        keys=[("tenant_id", ASCENDING), ("created_at", DESCENDING)],
        name="evaluations__tenant_created",
    ),
    # Lookup by analysis_id: "did we already evaluate this analysis?"
    IndexSpec(
        collection=Collection.EVALUATIONS,
        keys=[("tenant_id", ASCENDING), ("analysis_id", ASCENDING)],
        name="evaluations__tenant_analysis",
    ),
    # Flag-review queue: "which evaluations are flagged for human review?"
    IndexSpec(
        collection=Collection.EVALUATIONS,
        keys=[("tenant_id", ASCENDING), ("flagged", ASCENDING), ("created_at", DESCENDING)],
        name="evaluations__tenant_flagged",
        partial_filter_expression={"flagged": True},
    ),
    # TTL: evaluation results expire after 1 year.
    IndexSpec(
        collection=Collection.EVALUATIONS,
        keys=[("created_at", ASCENDING)],
        name="evaluations__ttl_created",
        expire_after_seconds=_TTL_365_DAYS,
    ),
    # =======================================================================
    # cost_ledger — append-only LLM billing events
    # =======================================================================
    # Primary aggregation query: "monthly spend for a tenant."
    # Index prefix (tenant_id, year_month) is implicit via created_at; we use
    # a compound (tenant_id, created_at) so the aggregation $match hits the
    # index. `job_id` appended for "per-job cost breakdown" queries.
    IndexSpec(
        collection=Collection.COST_LEDGER,
        keys=[("tenant_id", ASCENDING), ("created_at", DESCENDING)],
        name="cost_ledger__tenant_created",
    ),
    # Per-job drill-down: "what did this job cost?"
    IndexSpec(
        collection=Collection.COST_LEDGER,
        keys=[("tenant_id", ASCENDING), ("job_id", ASCENDING)],
        name="cost_ledger__tenant_job",
    ),
    # TTL: cost records kept for 90 days (billing reconciliation window).
    IndexSpec(
        collection=Collection.COST_LEDGER,
        keys=[("created_at", ASCENDING)],
        name="cost_ledger__ttl_created",
        expire_after_seconds=_TTL_90_DAYS,
    ),
    # =======================================================================
    # outbox — pending Kafka publishes (written inside Mongo transactions)
    # =======================================================================
    # The relay worker's hot query: "give me unpublished entries, oldest first."
    # Partial index covers only unpublished rows (published_at=null).
    # On a healthy system this is tiny; on a Kafka outage it may grow.
    IndexSpec(
        collection=Collection.OUTBOX,
        keys=[("published_at", ASCENDING), ("created_at", ASCENDING)],
        name="outbox__pending_created",
        partial_filter_expression={"published_at": None},
    ),
    # TTL: published entries can be pruned after 7 days (they've been relayed).
    # Unpublished entries (published_at=None) are NOT covered by this TTL
    # because the field is null; Mongo TTL only fires when the field value is
    # a date in the past.
    IndexSpec(
        collection=Collection.OUTBOX,
        keys=[("published_at", ASCENDING)],
        name="outbox__ttl_published",
        expire_after_seconds=7 * _SECONDS_PER_DAY,
    ),
    # =======================================================================
    # audit_log — append-only security event records (Phase 13)
    # =======================================================================
    # Primary query: "recent security events for a tenant."
    IndexSpec(
        collection=Collection.AUDIT_LOG,
        keys=[("tenant_id", ASCENDING), ("created_at", DESCENDING)],
        name="audit_log__tenant_created",
    ),
    # Filtered query: "events of a specific type for a tenant."
    IndexSpec(
        collection=Collection.AUDIT_LOG,
        keys=[("tenant_id", ASCENDING), ("event_type", ASCENDING), ("created_at", DESCENDING)],
        name="audit_log__tenant_event_type_created",
    ),
    # TTL: retain audit log for 365 days (regulatory retention minimum).
    IndexSpec(
        collection=Collection.AUDIT_LOG,
        keys=[("created_at", ASCENDING)],
        name="audit_log__ttl_created",
        expire_after_seconds=_TTL_365_DAYS,
    ),
    # =======================================================================
    # url_blocklists — per-tenant SSRF blocking rules (Phase 13)
    # =======================================================================
    # Primary query: "all blocklist rules for a tenant."
    IndexSpec(
        collection=Collection.URL_BLOCKLISTS,
        keys=[("tenant_id", ASCENDING), ("created_at", DESCENDING)],
        name="url_blocklists__tenant_created",
    ),
    # Dedup: prevent duplicate patterns per tenant.
    IndexSpec(
        collection=Collection.URL_BLOCKLISTS,
        keys=[("tenant_id", ASCENDING), ("pattern", ASCENDING)],
        name="url_blocklists__tenant_pattern_unique",
        unique=True,
    ),
    # =======================================================================
    # reviews — HITL review queue (Phase 13.5)
    # =======================================================================
    # Timeout sweeper: "find pending reviews whose deadline has passed."
    IndexSpec(
        collection=Collection.REVIEWS,
        keys=[("tenant_id", ASCENDING), ("status", ASCENDING), ("timeout_at", ASCENDING)],
        name="reviews__tenant_status_timeout",
    ),
    # Cross-tenant sweeper query (no tenant_id): "expired pending reviews globally."
    IndexSpec(
        collection=Collection.REVIEWS,
        keys=[("status", ASCENDING), ("timeout_at", ASCENDING)],
        name="reviews__status_timeout_sweeper",
        partial_filter_expression={"status": "pending"},
    ),
    # API list query: "this tenant's reviews, newest first, optionally by status."
    IndexSpec(
        collection=Collection.REVIEWS,
        keys=[("tenant_id", ASCENDING), ("created_at", DESCENDING)],
        name="reviews__tenant_created",
    ),
    # Uniqueness: only one PENDING review per job per tenant.
    # Partial index covers only pending rows — resolved reviews don't enforce this.
    IndexSpec(
        collection=Collection.REVIEWS,
        keys=[("tenant_id", ASCENDING), ("job_id", ASCENDING)],
        name="reviews__tenant_job_pending_unique",
        unique=True,
        partial_filter_expression={"status": "pending"},
    ),
    # TTL: retain resolved reviews for 365 days (audit/compliance).
    IndexSpec(
        collection=Collection.REVIEWS,
        keys=[("created_at", ASCENDING)],
        name="reviews__ttl_created",
        expire_after_seconds=_TTL_365_DAYS,
    ),
    # =======================================================================
    # tenant_settings — per-tenant operational config (Phase 13.5)
    # =======================================================================
    # One settings document per tenant; tenant_id is the natural unique key.
    IndexSpec(
        collection=Collection.TENANT_SETTINGS,
        keys=[("tenant_id", ASCENDING)],
        name="tenant_settings__tenant_unique",
        unique=True,
    ),
    # =======================================================================
    # memories — long-term episodic + semantic memory store (Phase 15.5)
    # =======================================================================
    # Query: "list active memories for a category by kind, newest first."
    # Primary retrieval index — used by MemoryRetriever and the API list endpoint.
    IndexSpec(
        collection=Collection.MEMORIES,
        keys=[
            ("tenant_id", ASCENDING),
            ("category_id", ASCENDING),
            ("kind", ASCENDING),
            ("_id", DESCENDING),
        ],
        name="memories__tenant_category_kind",
    ),
    # Partial index: active-only (is_active=True) — serves 99% of read traffic.
    # Superseded records are excluded here; the full index above covers auditing.
    IndexSpec(
        collection=Collection.MEMORIES,
        keys=[
            ("tenant_id", ASCENDING),
            ("category_id", ASCENDING),
            ("is_active", ASCENDING),
            ("kind", ASCENDING),
        ],
        name="memories__tenant_category_active",
        partial_filter_expression={"is_active": True},
    ),
    # Lookup by source_job_id: "which memories came from this job?"
    # Used by the consolidation worker idempotency check and the backfill script.
    IndexSpec(
        collection=Collection.MEMORIES,
        keys=[("tenant_id", ASCENDING), ("source_job_id", ASCENDING)],
        name="memories__tenant_job",
    ),
    # Supersede sweep: "find active memories this one supersedes."
    # Sparse because most memories have superseded_by=null.
    IndexSpec(
        collection=Collection.MEMORIES,
        keys=[("tenant_id", ASCENDING), ("superseded_by", ASCENDING)],
        name="memories__tenant_superseded_by",
        sparse=True,
    ),
    # TTL: 2 years. Memory is long-lived but not permanent.
    IndexSpec(
        collection=Collection.MEMORIES,
        keys=[("created_at", ASCENDING)],
        name="memories__ttl_created",
        expire_after_seconds=_TTL_365_DAYS * 2,
    ),
    # BM25 text index on content for keyword search over memories.
    IndexSpec(
        collection=Collection.MEMORIES,
        keys=[("content", TEXT)],  # type: ignore[list-item]
        name="memories__content_bm25",
    ),
    # Staleness detection — find least-recently-referenced active memories.
    IndexSpec(
        collection=Collection.MEMORIES,
        keys=[("tenant_id", ASCENDING), ("last_referenced_at", ASCENDING)],
        name="memories__tenant_last_referenced",
        sparse=True,
    ),
]


def indexes_for_collection(collection: Collection) -> list[IndexSpec]:
    """Filter the registry for one collection. Used by tests."""
    return [idx for idx in INDEXES if idx.collection == collection]
