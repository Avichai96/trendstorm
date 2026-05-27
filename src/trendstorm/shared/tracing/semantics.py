"""Span attribute key constants for TrendStorm business spans.

All span attribute keys used in manual instrumentation MUST come from this
module. Never use string literals for attribute keys in service or worker code.

Why constants instead of string literals?
    - Typos in attribute key strings cause silent data loss in traces.
    - A central module means renaming a key is a single-file change.
    - IDEs can navigate to the definition.

Naming convention follows OpenTelemetry semantic conventions where applicable
(https://opentelemetry.io/docs/specs/semconv/), with a "trendstorm." prefix
for domain-specific attributes.

Usage:
    from trendstorm.shared.tracing.semantics import Attr
    with tracer.start_as_current_span("scout.fetch", attributes={
        Attr.TENANT_ID: tenant_id,
        Attr.SOURCE_ID: source_id,
        Attr.HTTP_URL: str(url),
    }):
        ...
"""
from __future__ import annotations


class Attr:
    """Span attribute key constants. All values are strings."""

    # ------------------------------------------------------------------ #
    # Identity (high-cardinality — span attributes only, never metric labels)
    # ------------------------------------------------------------------ #
    TENANT_ID = "trendstorm.tenant_id"
    JOB_ID = "trendstorm.job_id"
    CATEGORY_ID = "trendstorm.category_id"
    SOURCE_ID = "trendstorm.source_id"
    DOCUMENT_ID = "trendstorm.document_id"
    CHUNK_ID = "trendstorm.chunk_id"
    ANALYSIS_ID = "trendstorm.analysis_id"
    REPORT_ID = "trendstorm.report_id"
    CORRELATION_ID = "trendstorm.correlation_id"

    # ------------------------------------------------------------------ #
    # Pipeline stage
    # ------------------------------------------------------------------ #
    STAGE = "trendstorm.stage"
    REFINEMENT_LOOP = "trendstorm.refinement_loop"

    # ------------------------------------------------------------------ #
    # Scout / ingestion
    # ------------------------------------------------------------------ #
    HTTP_URL = "http.url"                    # reuses OTel semconv key
    HTTP_STATUS_CODE = "http.status_code"    # reuses OTel semconv key
    CONTENT_TYPE = "trendstorm.content_type"
    BYTES_FETCHED = "trendstorm.bytes_fetched"
    SOURCE_HOST = "trendstorm.source_host"
    PARSE_RESULT = "trendstorm.parse_result"   # "ok" | "empty" | "error"

    # ------------------------------------------------------------------ #
    # Knowledge / embedding
    # ------------------------------------------------------------------ #
    CHUNK_COUNT = "trendstorm.chunk_count"
    PARENT_CHUNK_COUNT = "trendstorm.parent_chunk_count"
    CHILD_CHUNK_COUNT = "trendstorm.child_chunk_count"
    BATCH_SIZE = "trendstorm.batch_size"
    VECTOR_COUNT = "trendstorm.vector_count"
    EMBEDDING_MODEL = "trendstorm.embedding_model"
    EMBEDDING_DIMENSIONS = "trendstorm.embedding_dimensions"

    # ------------------------------------------------------------------ #
    # Retrieval
    # ------------------------------------------------------------------ #
    QUERY = "trendstorm.query"
    SUB_QUERY_COUNT = "trendstorm.sub_query_count"
    BM25_HITS = "trendstorm.bm25_hits"
    VECTOR_HITS = "trendstorm.vector_hits"
    AFTER_RRF_COUNT = "trendstorm.after_rrf_count"
    AFTER_RERANK_COUNT = "trendstorm.after_rerank_count"
    PARENT_CONTEXT_COUNT = "trendstorm.parent_context_count"
    RETRIEVAL_K = "trendstorm.retrieval_k"
    RERANKER_USED = "trendstorm.reranker_used"

    # ------------------------------------------------------------------ #
    # LLM calls
    # ------------------------------------------------------------------ #
    MODEL_ID = "trendstorm.model_id"
    MODEL_PROVIDER = "trendstorm.model_provider"
    OPERATION = "trendstorm.operation"        # "analyst_chat" | "validator_chat" | etc.
    INPUT_TOKENS = "trendstorm.input_tokens"
    OUTPUT_TOKENS = "trendstorm.output_tokens"
    CACHED_TOKENS = "trendstorm.cached_tokens"
    FINISH_REASON = "trendstorm.finish_reason"
    VALIDATOR_SCORE = "trendstorm.validator_score"
    VALIDATOR_PASSED = "trendstorm.validator_passed"

    # ------------------------------------------------------------------ #
    # Publisher
    # ------------------------------------------------------------------ #
    REPORT_FORMAT = "trendstorm.report_format"   # "md" | "json" | "pdf"
    REPORT_BYTES = "trendstorm.report_bytes"
    PDF_SUCCESS = "trendstorm.pdf_success"

    # ------------------------------------------------------------------ #
    # SSE / streaming
    # ------------------------------------------------------------------ #
    STREAM_EVENT_TYPE = "trendstorm.stream_event_type"
    SSE_SEQ = "trendstorm.sse_seq"
    SSE_CHANNEL = "trendstorm.sse_channel"

    # ------------------------------------------------------------------ #
    # Error context
    # ------------------------------------------------------------------ #
    ERROR_CODE = "trendstorm.error_code"
    ERROR_CLASS = "trendstorm.error_class"
    IS_PERMANENT = "trendstorm.is_permanent"
    ATTEMPT = "trendstorm.attempt"

    # ------------------------------------------------------------------ #
    # Security (Phase 13)
    # ------------------------------------------------------------------ #
    SECURITY_BLOCK_REASON = "trendstorm.security.block_reason"
    SECURITY_BLOCKED_URL = "trendstorm.security.blocked_url"
    SECURITY_BLOCKED_HOST = "trendstorm.security.blocked_host"
    SECURITY_PII_TYPE = "trendstorm.security.pii_type"
    SECURITY_PII_COUNT = "trendstorm.security.pii_count"
    SECURITY_AUDIT_EVENT_TYPE = "trendstorm.security.audit_event_type"
    SECURITY_REDIRECT_HOP = "trendstorm.security.redirect_hop"
