"""Central Prometheus metrics registry for TrendStorm AI.

ALL metrics are declared here. No service imports prometheus_client directly —
they go through this module so cardinality is enforced in one place.

CARDINALITY RULES (enforced by _FORBIDDEN_LABELS):
    HIGH-CARDINALITY IDENTIFIERS go only in trace attributes and log fields.
    NEVER use job_id, document_id, chunk_id, correlation_id, source_id,
    or any other unbounded identifier as a Prometheus label.

    Allowed label dimensions (bounded):
        tenant_id   — bounded by number of tenants (target: ≤ 1000)
        service     — bounded by number of services (6)
        stage       — bounded by pipeline stages (8)
        status      — success | error | permanent_error | skipped
        model_id    — bounded by LLM model catalogue (target: ≤ 20)
        provider    — anthropic | openai | ollama | gemini | cohere
        operation   — bounded per service (≤ 10)
        content_type — bounded MIME type categories (≤ 8)
        format      — md | json | pdf

Label max-cardinality estimates (used in tests):
    JOB_DURATION:      tenants(1000) x stages(8) x status(4) = 32 000 (warn)
    Actually staged per SLO:
        API:           tenants(1000) x operations(10) x status(4) = 40 000
        SCOUT:         tenants(1000) x status(4) = 4 000
        KNOWLEDGE:     tenants(1000) x status(4) = 4 000
        ANALYST:       tenants(1000) x models(20) x status(4) = 80 000
        PUBLISHER:     tenants(1000) x format(3) x status(4) = 12 000
        LLM:           tenants(1000) x models(20) x provider(5) x op(10) = 1 000 000
            → LLM metrics use (tenant_id, model_id, provider, operation) with
              STRICT model_id enum (only declared models pass).

Naming convention:
    trendstorm_{service}_{noun}_{unit}[_total]
    Histogram:  *_duration_seconds (no suffix)
    Counter:    *_total (auto-suffix from prometheus_client)
    Gauge:      *_{noun} (no suffix)

Usage:
    from trendstorm.shared.metrics.registry import METRICS
    METRICS.api_request_duration.labels(tenant_id=tid, operation="create_job", status="success").observe(elapsed)
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

# ---------------------------------------------------------------------------
# Forbidden label guard
# ---------------------------------------------------------------------------

_FORBIDDEN_LABELS: Final[frozenset[str]] = frozenset(
    {
        "job_id",
        "document_id",
        "chunk_id",
        "correlation_id",
        "source_id",
        "analysis_id",
        "report_id",
        "user_id",
        "request_id",
    }
)


def _check_labels(metric_name: str, labels: tuple[str, ...]) -> None:
    """Raise at import time if any label is in the forbidden set."""
    bad = frozenset(labels) & _FORBIDDEN_LABELS
    if bad:
        raise ValueError(
            f"Metric {metric_name!r} uses high-cardinality label(s) {bad!r}. "
            "Use trace attributes or log fields for these identifiers."
        )


# ---------------------------------------------------------------------------
# Allowed label value enums
# ---------------------------------------------------------------------------


class StatusLabel(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    PERMANENT_ERROR = "permanent_error"
    SKIPPED = "skipped"


class ProviderLabel(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    OLLAMA = "ollama"
    GEMINI = "gemini"
    COHERE = "cohere"


class FormatLabel(StrEnum):
    MARKDOWN = "md"
    JSON = "json"
    PDF = "pdf"


class ContentTypeLabel(StrEnum):
    HTML = "html"
    RSS = "rss"
    JSON = "json"
    SITEMAP = "sitemap"
    PDF = "pdf"
    XML = "xml"
    PLAIN = "plain"
    OTHER = "other"


class MemoryKindLabel(StrEnum):
    """Memory kind label values — bounded by enum for cardinality safety."""

    SEMANTIC = "semantic"
    EPISODIC = "episodic"


class SecurityBlockReason(StrEnum):
    """Bounded set of SSRF/security block reasons used as Prometheus label values.

    Each value corresponds to a rejection class in infrastructure/security/*.
    Keeping these as a StrEnum gives IDE completion and prevents typos at
    call sites. New rejection classes MUST be added here first.
    """

    # SSRF — IPv4
    SSRF_PRIVATE_IP = "ssrf_private_ip"  # RFC 1918 + CG-NAT
    SSRF_LOOPBACK = "ssrf_loopback"  # 127.0.0.0/8
    SSRF_LINK_LOCAL = "ssrf_link_local"  # 169.254.0.0/16 (AWS IMDS)
    SSRF_IPV4_MAPPED = "ssrf_ipv4_mapped"  # ::ffff:0:0/96
    # SSRF — IPv6
    SSRF_IPV6_LOOPBACK = "ssrf_ipv6_loopback"  # ::1
    SSRF_IPV6_ULA = "ssrf_ipv6_ula"  # fc00::/7
    SSRF_IPV6_LINK_LOCAL = "ssrf_ipv6_link_local"  # fe80::/10
    # SSRF — other
    SSRF_INTERNAL_HOSTNAME = "ssrf_internal_hostname"  # .internal / .local etc.
    SSRF_SCHEME_DOWNGRADE = "ssrf_scheme_downgrade"  # https -> http redirect
    SSRF_SCHEME_NOT_ALLOWED = "ssrf_scheme_not_allowed"  # file://, ftp:// etc.
    SSRF_MAX_REDIRECTS = "ssrf_max_redirects"  # exceeded hop limit
    SSRF_DNS_FAILURE = "ssrf_dns_failure"  # hostname did not resolve
    SSRF_NO_HOSTNAME = "ssrf_no_hostname"  # URL has no hostname
    # Blocklists
    SSRF_BLOCKLIST_GLOBAL = "ssrf_blocklist_global"  # global ops/security file
    SSRF_BLOCKLIST_TENANT = "ssrf_blocklist_tenant"  # per-tenant Mongo entry
    # PII
    PII_SSN = "pii_ssn"
    PII_CC = "pii_cc"
    PII_EMAIL = "pii_email"
    PII_PHONE = "pii_phone"
    PII_IBAN = "pii_iban"


# ---------------------------------------------------------------------------
# Histogram bucket presets
# ---------------------------------------------------------------------------

# Sub-second operations (API, SSE, Redis writes)
_FAST_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

# Medium operations (fetch, parse, upload, chunk)
_MEDIUM_BUCKETS = (0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0)

# Slow operations (full analyst pass, full job)
_SLOW_BUCKETS = (5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0, 900.0, 1800.0)


# ---------------------------------------------------------------------------
# Registry factory
# ---------------------------------------------------------------------------


class _TrendStormMetrics:
    """Singleton container for all declared metrics.

    Instantiated once at module level. All attributes are set in __init__
    so IDEs and type checkers can navigate to metric declarations.
    """

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        # Allow injecting a separate registry for tests so metrics don't leak
        # between test runs. In production, registry=None uses the global one.
        # We use explicit conditional calls rather than **kw unpacking because
        # prometheus_client stubs do not model registry= as a valid kwarg via
        # **dict[str, CollectorRegistry] expansion.

        # ------------------------------------------------------------------ #
        # API metrics
        # ------------------------------------------------------------------ #
        _check_labels("api_request_duration_seconds", ("tenant_id", "operation", "status"))
        self.api_request_duration = (
            Histogram(
                "trendstorm_api_request_duration_seconds",
                "Latency of API requests (non-streaming) by operation.",
                labelnames=["tenant_id", "operation", "status"],
                buckets=_FAST_BUCKETS,
                registry=registry,
            )
            if registry is not None
            else Histogram(
                "trendstorm_api_request_duration_seconds",
                "Latency of API requests (non-streaming) by operation.",
                labelnames=["tenant_id", "operation", "status"],
                buckets=_FAST_BUCKETS,
            )
        )

        _check_labels("api_requests_total", ("tenant_id", "operation", "status"))
        self.api_requests = (
            Counter(
                "trendstorm_api_requests_total",
                "Total API requests by operation and outcome.",
                labelnames=["tenant_id", "operation", "status"],
                registry=registry,
            )
            if registry is not None
            else Counter(
                "trendstorm_api_requests_total",
                "Total API requests by operation and outcome.",
                labelnames=["tenant_id", "operation", "status"],
            )
        )

        # ------------------------------------------------------------------ #
        # Job lifecycle metrics
        # ------------------------------------------------------------------ #
        _check_labels("job_duration_seconds", ("tenant_id", "status"))
        self.job_duration = (
            Histogram(
                "trendstorm_job_duration_seconds",
                "End-to-end job duration from requested to terminal state.",
                labelnames=["tenant_id", "status"],
                buckets=_SLOW_BUCKETS,
                registry=registry,
            )
            if registry is not None
            else Histogram(
                "trendstorm_job_duration_seconds",
                "End-to-end job duration from requested to terminal state.",
                labelnames=["tenant_id", "status"],
                buckets=_SLOW_BUCKETS,
            )
        )

        _check_labels("jobs_total", ("tenant_id", "status"))
        self.jobs = (
            Counter(
                "trendstorm_jobs_total",
                "Total jobs by terminal status.",
                labelnames=["tenant_id", "status"],
                registry=registry,
            )
            if registry is not None
            else Counter(
                "trendstorm_jobs_total",
                "Total jobs by terminal status.",
                labelnames=["tenant_id", "status"],
            )
        )

        # ------------------------------------------------------------------ #
        # Scout worker metrics
        # ------------------------------------------------------------------ #
        _check_labels("scout_source_duration_seconds", ("tenant_id", "status"))
        self.scout_source_duration = (
            Histogram(
                "trendstorm_scout_source_duration_seconds",
                "Time to fetch, parse, and upload a single source.",
                labelnames=["tenant_id", "status"],
                buckets=_MEDIUM_BUCKETS,
                registry=registry,
            )
            if registry is not None
            else Histogram(
                "trendstorm_scout_source_duration_seconds",
                "Time to fetch, parse, and upload a single source.",
                labelnames=["tenant_id", "status"],
                buckets=_MEDIUM_BUCKETS,
            )
        )

        _check_labels("scout_sources_total", ("tenant_id", "content_type", "status"))
        self.scout_sources = (
            Counter(
                "trendstorm_scout_sources_total",
                "Total sources processed by content type and outcome.",
                labelnames=["tenant_id", "content_type", "status"],
                registry=registry,
            )
            if registry is not None
            else Counter(
                "trendstorm_scout_sources_total",
                "Total sources processed by content type and outcome.",
                labelnames=["tenant_id", "content_type", "status"],
            )
        )

        _check_labels("scout_bytes_fetched_total", ("tenant_id",))
        self.scout_bytes_fetched = (
            Counter(
                "trendstorm_scout_bytes_fetched_total",
                "Total raw bytes fetched across all sources.",
                labelnames=["tenant_id"],
                registry=registry,
            )
            if registry is not None
            else Counter(
                "trendstorm_scout_bytes_fetched_total",
                "Total raw bytes fetched across all sources.",
                labelnames=["tenant_id"],
            )
        )

        # ------------------------------------------------------------------ #
        # Knowledge worker metrics
        # ------------------------------------------------------------------ #
        _check_labels("knowledge_document_duration_seconds", ("tenant_id", "status"))
        self.knowledge_document_duration = (
            Histogram(
                "trendstorm_knowledge_document_duration_seconds",
                "Time to chunk, embed, and upsert a single document.",
                labelnames=["tenant_id", "status"],
                buckets=_MEDIUM_BUCKETS,
                registry=registry,
            )
            if registry is not None
            else Histogram(
                "trendstorm_knowledge_document_duration_seconds",
                "Time to chunk, embed, and upsert a single document.",
                labelnames=["tenant_id", "status"],
                buckets=_MEDIUM_BUCKETS,
            )
        )

        _check_labels("knowledge_chunks_created_total", ("tenant_id",))
        self.knowledge_chunks_created = (
            Counter(
                "trendstorm_knowledge_chunks_created_total",
                "Total chunk documents inserted into Mongo.",
                labelnames=["tenant_id"],
                registry=registry,
            )
            if registry is not None
            else Counter(
                "trendstorm_knowledge_chunks_created_total",
                "Total chunk documents inserted into Mongo.",
                labelnames=["tenant_id"],
            )
        )

        _check_labels("knowledge_vectors_upserted_total", ("tenant_id", "model_id"))
        self.knowledge_vectors_upserted = (
            Counter(
                "trendstorm_knowledge_vectors_upserted_total",
                "Total vectors upserted to the vector store.",
                labelnames=["tenant_id", "model_id"],
                registry=registry,
            )
            if registry is not None
            else Counter(
                "trendstorm_knowledge_vectors_upserted_total",
                "Total vectors upserted to the vector store.",
                labelnames=["tenant_id", "model_id"],
            )
        )

        _check_labels("knowledge_embed_batch_duration_seconds", ("tenant_id", "model_id"))
        self.knowledge_embed_batch_duration = (
            Histogram(
                "trendstorm_knowledge_embed_batch_duration_seconds",
                "Time to embed a single batch of chunks.",
                labelnames=["tenant_id", "model_id"],
                buckets=_FAST_BUCKETS,
                registry=registry,
            )
            if registry is not None
            else Histogram(
                "trendstorm_knowledge_embed_batch_duration_seconds",
                "Time to embed a single batch of chunks.",
                labelnames=["tenant_id", "model_id"],
                buckets=_FAST_BUCKETS,
            )
        )

        # ------------------------------------------------------------------ #
        # Analyst worker metrics
        # ------------------------------------------------------------------ #
        _check_labels("analyst_pass_duration_seconds", ("tenant_id", "status"))
        self.analyst_pass_duration = (
            Histogram(
                "trendstorm_analyst_pass_duration_seconds",
                "Time for a complete analyst pass (retrieve + LLM + validate).",
                labelnames=["tenant_id", "status"],
                buckets=_SLOW_BUCKETS,
                registry=registry,
            )
            if registry is not None
            else Histogram(
                "trendstorm_analyst_pass_duration_seconds",
                "Time for a complete analyst pass (retrieve + LLM + validate).",
                labelnames=["tenant_id", "status"],
                buckets=_SLOW_BUCKETS,
            )
        )

        _check_labels("analyst_passes_total", ("tenant_id", "status"))
        self.analyst_passes = (
            Counter(
                "trendstorm_analyst_passes_total",
                "Total analyst passes by outcome.",
                labelnames=["tenant_id", "status"],
                registry=registry,
            )
            if registry is not None
            else Counter(
                "trendstorm_analyst_passes_total",
                "Total analyst passes by outcome.",
                labelnames=["tenant_id", "status"],
            )
        )

        _check_labels("analyst_retrieval_hits", ("tenant_id", "backend"))
        self.analyst_retrieval_hits = (
            Histogram(
                "trendstorm_analyst_retrieval_hits",
                "Number of chunks returned by each retrieval backend.",
                labelnames=["tenant_id", "backend"],
                buckets=(0, 1, 5, 10, 20, 30, 50, 100),
                registry=registry,
            )
            if registry is not None
            else Histogram(
                "trendstorm_analyst_retrieval_hits",
                "Number of chunks returned by each retrieval backend.",
                labelnames=["tenant_id", "backend"],
                buckets=(0, 1, 5, 10, 20, 30, 50, 100),
            )
        )

        _check_labels("analyst_refinement_loops_total", ("tenant_id",))
        self.analyst_refinement_loops = (
            Counter(
                "trendstorm_analyst_refinement_loops_total",
                "Total number of refinement loops triggered.",
                labelnames=["tenant_id"],
                registry=registry,
            )
            if registry is not None
            else Counter(
                "trendstorm_analyst_refinement_loops_total",
                "Total number of refinement loops triggered.",
                labelnames=["tenant_id"],
            )
        )

        # ------------------------------------------------------------------ #
        # LLM call metrics (all providers)
        # ------------------------------------------------------------------ #
        _check_labels(
            "llm_call_duration_seconds", ("tenant_id", "provider", "model_id", "operation")
        )
        self.llm_call_duration = (
            Histogram(
                "trendstorm_llm_call_duration_seconds",
                "Time for a single LLM API call.",
                labelnames=["tenant_id", "provider", "model_id", "operation"],
                buckets=_SLOW_BUCKETS,
                registry=registry,
            )
            if registry is not None
            else Histogram(
                "trendstorm_llm_call_duration_seconds",
                "Time for a single LLM API call.",
                labelnames=["tenant_id", "provider", "model_id", "operation"],
                buckets=_SLOW_BUCKETS,
            )
        )

        _check_labels(
            "llm_calls_total", ("tenant_id", "provider", "model_id", "operation", "status")
        )
        self.llm_calls = (
            Counter(
                "trendstorm_llm_calls_total",
                "Total LLM calls by provider, model, operation, and outcome.",
                labelnames=["tenant_id", "provider", "model_id", "operation", "status"],
                registry=registry,
            )
            if registry is not None
            else Counter(
                "trendstorm_llm_calls_total",
                "Total LLM calls by provider, model, operation, and outcome.",
                labelnames=["tenant_id", "provider", "model_id", "operation", "status"],
            )
        )

        _check_labels("llm_input_tokens_total", ("tenant_id", "provider", "model_id", "operation"))
        self.llm_input_tokens = (
            Counter(
                "trendstorm_llm_input_tokens_total",
                "Total input tokens consumed, for cost attribution.",
                labelnames=["tenant_id", "provider", "model_id", "operation"],
                registry=registry,
            )
            if registry is not None
            else Counter(
                "trendstorm_llm_input_tokens_total",
                "Total input tokens consumed, for cost attribution.",
                labelnames=["tenant_id", "provider", "model_id", "operation"],
            )
        )

        _check_labels("llm_output_tokens_total", ("tenant_id", "provider", "model_id", "operation"))
        self.llm_output_tokens = (
            Counter(
                "trendstorm_llm_output_tokens_total",
                "Total output tokens generated, for cost attribution.",
                labelnames=["tenant_id", "provider", "model_id", "operation"],
                registry=registry,
            )
            if registry is not None
            else Counter(
                "trendstorm_llm_output_tokens_total",
                "Total output tokens generated, for cost attribution.",
                labelnames=["tenant_id", "provider", "model_id", "operation"],
            )
        )

        _check_labels("llm_cached_tokens_total", ("tenant_id", "provider", "model_id", "operation"))
        self.llm_cached_tokens = (
            Counter(
                "trendstorm_llm_cached_tokens_total",
                "Total prompt-cached tokens (Anthropic cache_read_input_tokens).",
                labelnames=["tenant_id", "provider", "model_id", "operation"],
                registry=registry,
            )
            if registry is not None
            else Counter(
                "trendstorm_llm_cached_tokens_total",
                "Total prompt-cached tokens (Anthropic cache_read_input_tokens).",
                labelnames=["tenant_id", "provider", "model_id", "operation"],
            )
        )

        # ------------------------------------------------------------------ #
        # Publisher worker metrics
        # ------------------------------------------------------------------ #
        _check_labels("publisher_render_duration_seconds", ("tenant_id", "format", "status"))
        self.publisher_render_duration = (
            Histogram(
                "trendstorm_publisher_render_duration_seconds",
                "Time to render a single report format.",
                labelnames=["tenant_id", "format", "status"],
                buckets=_MEDIUM_BUCKETS,
                registry=registry,
            )
            if registry is not None
            else Histogram(
                "trendstorm_publisher_render_duration_seconds",
                "Time to render a single report format.",
                labelnames=["tenant_id", "format", "status"],
                buckets=_MEDIUM_BUCKETS,
            )
        )

        _check_labels("publisher_renders_total", ("tenant_id", "format", "status"))
        self.publisher_renders = (
            Counter(
                "trendstorm_publisher_renders_total",
                "Total report renders by format and outcome.",
                labelnames=["tenant_id", "format", "status"],
                registry=registry,
            )
            if registry is not None
            else Counter(
                "trendstorm_publisher_renders_total",
                "Total report renders by format and outcome.",
                labelnames=["tenant_id", "format", "status"],
            )
        )

        _check_labels("publisher_bytes_uploaded_total", ("tenant_id", "format"))
        self.publisher_bytes_uploaded = (
            Counter(
                "trendstorm_publisher_bytes_uploaded_total",
                "Total bytes uploaded to blob storage by format.",
                labelnames=["tenant_id", "format"],
                registry=registry,
            )
            if registry is not None
            else Counter(
                "trendstorm_publisher_bytes_uploaded_total",
                "Total bytes uploaded to blob storage by format.",
                labelnames=["tenant_id", "format"],
            )
        )

        # ------------------------------------------------------------------ #
        # SSE coordinator metrics
        # ------------------------------------------------------------------ #
        _check_labels("sse_event_duration_seconds", ("tenant_id", "status"))
        self.sse_event_duration = (
            Histogram(
                "trendstorm_sse_event_duration_seconds",
                "Time from Kafka consume to Redis Streams write for SSE events.",
                labelnames=["tenant_id", "status"],
                buckets=_FAST_BUCKETS,
                registry=registry,
            )
            if registry is not None
            else Histogram(
                "trendstorm_sse_event_duration_seconds",
                "Time from Kafka consume to Redis Streams write for SSE events.",
                labelnames=["tenant_id", "status"],
                buckets=_FAST_BUCKETS,
            )
        )

        _check_labels("sse_events_total", ("tenant_id", "event_type", "status"))
        self.sse_events = (
            Counter(
                "trendstorm_sse_events_total",
                "Total SSE events processed by type and outcome.",
                labelnames=["tenant_id", "event_type", "status"],
                registry=registry,
            )
            if registry is not None
            else Counter(
                "trendstorm_sse_events_total",
                "Total SSE events processed by type and outcome.",
                labelnames=["tenant_id", "event_type", "status"],
            )
        )

        # ------------------------------------------------------------------ #
        # Orchestrator metrics
        # ------------------------------------------------------------------ #
        _check_labels("orchestrator_transitions_total", ("tenant_id", "from_stage", "to_stage"))
        self.orchestrator_transitions = (
            Counter(
                "trendstorm_orchestrator_transitions_total",
                "Total stage transitions executed by the orchestrator.",
                labelnames=["tenant_id", "from_stage", "to_stage"],
                registry=registry,
            )
            if registry is not None
            else Counter(
                "trendstorm_orchestrator_transitions_total",
                "Total stage transitions executed by the orchestrator.",
                labelnames=["tenant_id", "from_stage", "to_stage"],
            )
        )

        _check_labels("orchestrator_events_total", ("tenant_id", "event_type", "status"))
        self.orchestrator_events = (
            Counter(
                "trendstorm_orchestrator_events_total",
                "Total events handled by the orchestrator by type and outcome.",
                labelnames=["tenant_id", "event_type", "status"],
                registry=registry,
            )
            if registry is not None
            else Counter(
                "trendstorm_orchestrator_events_total",
                "Total events handled by the orchestrator by type and outcome.",
                labelnames=["tenant_id", "event_type", "status"],
            )
        )

        # ------------------------------------------------------------------ #
        # Infrastructure health gauges
        # ------------------------------------------------------------------ #
        _check_labels("mongo_pool_utilization_ratio", ("service",))
        self.mongo_pool_utilization = (
            Gauge(
                "trendstorm_mongo_pool_utilization_ratio",
                "Fraction of Mongo connection pool slots currently in use.",
                labelnames=["service"],
                registry=registry,
            )
            if registry is not None
            else Gauge(
                "trendstorm_mongo_pool_utilization_ratio",
                "Fraction of Mongo connection pool slots currently in use.",
                labelnames=["service"],
            )
        )

        _check_labels("vector_store_health", ("service",))
        self.vector_store_health = (
            Gauge(
                "trendstorm_vector_store_health",
                "1 if ChromaDB health check passed, 0 otherwise.",
                labelnames=["service"],
                registry=registry,
            )
            if registry is not None
            else Gauge(
                "trendstorm_vector_store_health",
                "1 if ChromaDB health check passed, 0 otherwise.",
                labelnames=["service"],
            )
        )

        _check_labels("kafka_consumer_lag_messages", ("service", "consumer_group", "topic"))
        self.kafka_consumer_lag = (
            Gauge(
                "trendstorm_kafka_consumer_lag_messages",
                "Current Kafka consumer lag in messages.",
                labelnames=["service", "consumer_group", "topic"],
                registry=registry,
            )
            if registry is not None
            else Gauge(
                "trendstorm_kafka_consumer_lag_messages",
                "Current Kafka consumer lag in messages.",
                labelnames=["service", "consumer_group", "topic"],
            )
        )

        # ------------------------------------------------------------------ #
        # Security metrics (Phase 13)
        # ------------------------------------------------------------------ #
        # tenant_id_hash is a bucketed hash (not raw tenant_id) so cardinality
        # is bounded at 100 buckets regardless of tenant count.
        # reason is drawn from SecurityBlockReason (closed enum).
        _check_labels("security_block_total", ("reason", "tenant_id_hash"))
        self.security_blocks = (
            Counter(
                "trendstorm_security_block_total",
                "Total security blocks by reason and hashed tenant bucket.",
                labelnames=["reason", "tenant_id_hash"],
                registry=registry,
            )
            if registry is not None
            else Counter(
                "trendstorm_security_block_total",
                "Total security blocks by reason and hashed tenant bucket.",
                labelnames=["reason", "tenant_id_hash"],
            )
        )

        # ------------------------------------------------------------------ #
        # HITL review metrics (Phase 13.5)
        # ------------------------------------------------------------------ #
        # Gauge: how many reviews are currently pending (per tenant hash bucket).
        _check_labels("reviews_pending", ("tenant_id_hash",))
        self.reviews_pending = (
            Gauge(
                "trendstorm_reviews_pending",
                "Number of pending HITL reviews awaiting a decision.",
                labelnames=["tenant_id_hash"],
                registry=registry,
            )
            if registry is not None
            else Gauge(
                "trendstorm_reviews_pending",
                "Number of pending HITL reviews awaiting a decision.",
                labelnames=["tenant_id_hash"],
            )
        )
        # Histogram: wall-clock seconds from review created_at to resolved_at.
        _check_labels("review_resolution_seconds", ("decision",))
        self.review_resolution_seconds = (
            Histogram(
                "trendstorm_review_resolution_seconds",
                "Time in seconds from review creation to resolution.",
                labelnames=["decision"],
                buckets=[60, 300, 900, 1800, 3600, 7200, 14400, 43200, 86400, 172800],
                registry=registry,
            )
            if registry is not None
            else Histogram(
                "trendstorm_review_resolution_seconds",
                "Time in seconds from review creation to resolution.",
                labelnames=["decision"],
                buckets=[60, 300, 900, 1800, 3600, 7200, 14400, 43200, 86400, 172800],
            )
        )
        # Counter: total reviews auto-expired by the timeout sweeper.
        self.review_timeout_total = (
            Counter(
                "trendstorm_review_timeout_total",
                "Total HITL reviews that expired without a reviewer decision.",
                registry=registry,
            )
            if registry is not None
            else Counter(
                "trendstorm_review_timeout_total",
                "Total HITL reviews that expired without a reviewer decision.",
            )
        )
        # Gauge: Unix timestamp of the oldest pending review per tenant hash bucket.
        # Used by the PendingReviewsAgingHigh alert: (time() - gauge) / 3600 > threshold.
        # Workers call .labels(tenant_id_hash=...).set(review.created_at.timestamp()).
        # When all pending reviews resolve, workers should .set(0) or the alert
        # expression naturally drops below the threshold.
        _check_labels("reviews_pending_oldest_created_at", ("tenant_id_hash",))
        self.reviews_pending_oldest_created_at = (
            Gauge(
                "trendstorm_reviews_pending_oldest_created_at",
                "Unix timestamp of the oldest pending HITL review per tenant bucket.",
                labelnames=["tenant_id_hash"],
                registry=registry,
            )
            if registry is not None
            else Gauge(
                "trendstorm_reviews_pending_oldest_created_at",
                "Unix timestamp of the oldest pending HITL review per tenant bucket.",
                labelnames=["tenant_id_hash"],
            )
        )

        # ------------------------------------------------------------------ #
        # Long-term memory metrics (Phase 15.5)
        # ------------------------------------------------------------------ #
        _check_labels("memory_writes_total", ("tenant_id", "kind", "status"))
        self.memory_writes = (
            Counter(
                "trendstorm_memory_writes_total",
                "Total memory records written by kind and outcome.",
                labelnames=["tenant_id", "kind", "status"],
                registry=registry,
            )
            if registry is not None
            else Counter(
                "trendstorm_memory_writes_total",
                "Total memory records written by kind and outcome.",
                labelnames=["tenant_id", "kind", "status"],
            )
        )

        _check_labels("memory_retrieval_hits", ("tenant_id", "kind"))
        self.memory_retrieval_hits = (
            Histogram(
                "trendstorm_memory_retrieval_hits",
                "Number of memories returned per retrieval call by kind.",
                labelnames=["tenant_id", "kind"],
                buckets=(0, 1, 2, 3, 5, 8, 10, 15, 20),
                registry=registry,
            )
            if registry is not None
            else Histogram(
                "trendstorm_memory_retrieval_hits",
                "Number of memories returned per retrieval call by kind.",
                labelnames=["tenant_id", "kind"],
                buckets=(0, 1, 2, 3, 5, 8, 10, 15, 20),
            )
        )

        _check_labels("memory_consolidation_duration_seconds", ("tenant_id", "status"))
        self.memory_consolidation_duration = (
            Histogram(
                "trendstorm_memory_consolidation_duration_seconds",
                "Time to extract and persist memories for one job.",
                labelnames=["tenant_id", "status"],
                buckets=_MEDIUM_BUCKETS,
                registry=registry,
            )
            if registry is not None
            else Histogram(
                "trendstorm_memory_consolidation_duration_seconds",
                "Time to extract and persist memories for one job.",
                labelnames=["tenant_id", "status"],
                buckets=_MEDIUM_BUCKETS,
            )
        )

        _check_labels("memories_active", ("tenant_id_hash", "kind"))
        self.memories_active = (
            Gauge(
                "trendstorm_memories_active",
                "Approximate count of active (non-superseded) memories per tenant bucket and kind.",
                labelnames=["tenant_id_hash", "kind"],
                registry=registry,
            )
            if registry is not None
            else Gauge(
                "trendstorm_memories_active",
                "Approximate count of active (non-superseded) memories per tenant bucket and kind.",
                labelnames=["tenant_id_hash", "kind"],
            )
        )

        # ------------------------------------------------------------------ #
        # Active SSE connections (saturation gauge)
        # ------------------------------------------------------------------ #
        _check_labels("sse_active_connections", ("service",))
        self.sse_active_connections = (
            Gauge(
                "trendstorm_sse_active_connections",
                "Number of active SSE streaming connections.",
                labelnames=["service"],
                registry=registry,
            )
            if registry is not None
            else Gauge(
                "trendstorm_sse_active_connections",
                "Number of active SSE streaming connections.",
                labelnames=["service"],
            )
        )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

METRICS = _TrendStormMetrics()


def tenant_id_hash(tenant_id: str) -> str:
    """Map a tenant_id to a bounded Prometheus label value.

    Returns a string like "b42" (100 buckets). Raw tenant_id must never
    appear as a Prometheus label (unbounded cardinality); this hash gives
    an aggregatable proxy while keeping cardinality at exactly 100.
    """
    return f"b{hash(tenant_id) % 100:02d}"


def record_security_block(
    reason: str,
    tenant_id: str,
    *,
    metrics: _TrendStormMetrics | None = None,
) -> None:
    """Increment trendstorm_security_block_total for one security rejection.

    Args:
        reason: A SecurityBlockReason value (or SSRFBlockedError.reason).
        tenant_id: The tenant scope — hashed before use as a label.
        metrics: Override for tests; defaults to the module-level METRICS singleton.

    """
    m = metrics if metrics is not None else METRICS
    try:
        m.security_blocks.labels(
            reason=reason,
            tenant_id_hash=tenant_id_hash(tenant_id),
        ).inc()
    except Exception:
        pass  # metric failure must never crash business logic


def make_test_metrics() -> _TrendStormMetrics:
    """Return a fresh metrics instance on an isolated registry for unit tests."""
    return _TrendStormMetrics(registry=CollectorRegistry())
