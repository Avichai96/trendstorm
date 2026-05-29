"""Kafka topic registry — single source of truth for topic names.

Every topic referenced anywhere in the code must be defined HERE, not as
a string literal at the call site. This prevents typos that silently create
new topics (in environments with auto-create) or fail at runtime.

Naming convention: `{org}.{domain}.{event}.{version}`
    org    = trendstorm
    domain = jobs | ingest | knowledge | analysis | publish | stream
    event  = past-tense verb (requested, completed, failed)
    version = v1, v2, ...

When evolving a topic schema:
    - Add a new topic (e.g. `.v2`).
    - Both producers and consumers run dual-write/dual-read for a deprecation
      window.
    - Eventually delete `.v1`.

NEVER mutate the schema of an existing version. Once a consumer is in prod
reading v1, you cannot change v1 without coordinated deploys (the whole
reason we use event-driven architecture in the first place).
"""

from __future__ import annotations

from enum import StrEnum


class Topic(StrEnum):
    """Canonical topic names. Mirror this set in `kafka-init` in compose."""

    # --- Main pipeline ---
    JOBS_REQUESTED = "trendstorm.jobs.requested.v1"
    INGEST_PENDING = "trendstorm.ingest.pending.v1"
    INGEST_COMPLETED = "trendstorm.ingest.completed.v1"
    KNOWLEDGE_PENDING = "trendstorm.knowledge.pending.v1"
    KNOWLEDGE_COMPLETED = "trendstorm.knowledge.completed.v1"
    ANALYSIS_PENDING = "trendstorm.analysis.pending.v1"
    ANALYSIS_COMPLETED = "trendstorm.analysis.completed.v1"
    PUBLISH_PENDING = "trendstorm.publish.pending.v1"
    PUBLISH_COMPLETED = "trendstorm.publish.completed.v1"

    # --- Streaming ---
    STREAM_PARTIAL = "trendstorm.stream.partial.v1"

    # --- Evaluation ---
    EVAL_SAMPLE = "trendstorm.eval.sample.v1"

    # --- Retries (delayed redelivery) ---
    RETRY_INGEST_30S = "trendstorm.retry.ingest.30s.v1"
    RETRY_INGEST_5M = "trendstorm.retry.ingest.5m.v1"
    RETRY_INGEST_1H = "trendstorm.retry.ingest.1h.v1"
    RETRY_KNOWLEDGE_30S = "trendstorm.retry.knowledge.30s.v1"
    RETRY_KNOWLEDGE_5M = "trendstorm.retry.knowledge.5m.v1"
    RETRY_KNOWLEDGE_1H = "trendstorm.retry.knowledge.1h.v1"
    RETRY_ANALYSIS_30S = "trendstorm.retry.analysis.30s.v1"
    RETRY_ANALYSIS_5M = "trendstorm.retry.analysis.5m.v1"
    RETRY_ANALYSIS_1H = "trendstorm.retry.analysis.1h.v1"
    RETRY_PUBLISH_30S = "trendstorm.retry.publish.30s.v1"
    RETRY_PUBLISH_5M = "trendstorm.retry.publish.5m.v1"
    RETRY_PUBLISH_1H = "trendstorm.retry.publish.1h.v1"

    # --- Dead letter ---
    DLQ = "trendstorm.dlq.v1"

    # --- HITL review (Phase 13.5) ---
    REVIEW_REQUESTED = "trendstorm.review.requested.v1"
    REVIEW_RESOLVED = "trendstorm.review.resolved.v1"

    # --- Long-term memory (Phase 15.5) ---
    MEMORY_PENDING = "trendstorm.memory.pending.v1"
    MEMORY_COMPLETED = "trendstorm.memory.completed.v1"
    RETRY_MEMORY_30S = "trendstorm.retry.memory.30s.v1"
    RETRY_MEMORY_5M = "trendstorm.retry.memory.5m.v1"
    RETRY_MEMORY_1H = "trendstorm.retry.memory.1h.v1"


class ConsumerGroup(StrEnum):
    """Canonical consumer group names.

    Group ID encodes the SUBSCRIBER, not the topic. Different groups consume
    independently; same group splits work.

    Convention: `{service}.{purpose}`
    """

    ORCHESTRATOR = "trendstorm.orchestrator"
    SCOUT = "trendstorm.scout"
    KNOWLEDGE = "trendstorm.knowledge"
    ANALYST = "trendstorm.analyst"
    PUBLISHER = "trendstorm.publisher"
    SSE_COORDINATOR = "trendstorm.sse-coordinator"
    PRODUCTION_EVAL = "trendstorm.production-eval"
    DLQ_MONITOR = "trendstorm.dlq-monitor"
    REVIEW_TIMEOUT = "trendstorm.review-timeout"
    MEMORY_CONSOLIDATION = "trendstorm.memory-consolidation"
