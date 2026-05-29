"""Orchestrator graph node functions.

A LangGraph node is just an async function: `(state) -> partial_state_update`.
Returning a dict (rather than a full JobState) tells LangGraph to MERGE the
returned keys into the state. We lean on Pydantic to validate that merge
produces a valid state.

Phase 4 status:
    All five domain nodes (ingest, embed, retrieve, analyze, publish) are
    STUBS that simulate work. They:
        - Log entry/exit.
        - Increment the attempts counter.
        - Sleep briefly to simulate work.
        - Update the appropriate state slice with placeholder data.
        - Occasionally simulate failure (controlled by env var for testing).

Phases 5-9 replace each stub with a real implementation:
    Phase 5: persist real Job + JobState changes to Mongo.
    Phase 6: ingest_node calls the real Scout agent (crawler + parser).
    Phase 7: embed_node calls the real Knowledge agent (chunker + embedder).
    Phase 8: retrieve_node + analyze_node use hybrid RAG.
    Phase 9: publish_node renders the report.
"""

from __future__ import annotations

import asyncio
import os
import random
from typing import TYPE_CHECKING, Any

from opentelemetry import trace

from trendstorm.agents.stages import Stage, is_valid_transition
from trendstorm.agents.state import (
    AnalysisState,
    ChunkRef,
    DocumentRef,
    IngestionState,
    JobState,
    KnowledgeState,
    MemoryConsolidationState,
    PublishingState,
    RetrievalState,
)
from trendstorm.infrastructure.blob.uri import parse_s3_uri, to_s3_uri
from trendstorm.orchestration.events import (
    AnalysisPendingEvent,
    IngestPendingEvent,
    KnowledgeDocRef,
    KnowledgePendingEvent,
    MemoryPendingEvent,
    PublishPendingEvent,
)
from trendstorm.orchestration.topics import Topic  # REVIEW_REQUESTED used inside review_gate_node
from trendstorm.shared.errors import ExternalServiceError
from trendstorm.shared.ids import new_id
from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)


# Test hook: set TRENDSTORM_STUB_FAIL_PROB=0.5 to inject failures.
# Used in integration tests to exercise the retry loop.
_FAIL_PROB = float(os.getenv("TRENDSTORM_STUB_FAIL_PROB", "0.0"))


# ===========================================================================
# Helpers
# ===========================================================================


def _increment_attempt(state: JobState, stage: Stage) -> int:
    """Bump and return the attempt counter for a stage."""
    n = state.attempts.get(stage, 0) + 1
    return n


def _record_transition(state: JobState, to_stage: Stage) -> dict[str, Any]:
    """Return a state-update dict that transitions to `to_stage`."""
    if not is_valid_transition(state.stage, to_stage):
        # Programming error — log and fail-fast in dev. In prod we'd want
        # to mark the job failed rather than crash the worker.
        raise ValueError(f"Invalid transition: {state.stage.value} -> {to_stage.value}")
    return {"stage": to_stage}


async def _maybe_fail(stage: Stage) -> None:
    """For integration tests: simulate transient failures."""
    if _FAIL_PROB > 0 and random.random() < _FAIL_PROB:  # noqa: S311
        raise ExternalServiceError(
            f"Simulated {stage.value} failure",
            code=f"simulated_{stage.value}_failure",
        )


# ===========================================================================
# Node: init_job  — first node, marks job as INGESTING
# ===========================================================================


async def init_job(state: JobState) -> dict[str, Any]:
    """Transition PENDING -> INGESTING and set up the job for ingestion.

    This exists as a separate node (rather than starting at INGESTING) so
    that the graph entry has a clean transition target and we can attach
    setup work later (e.g. acquiring resources, emitting "job_started" event).
    """
    with tracer.start_as_current_span("orchestrator.init_job"):
        logger.info("node_init_job", job_id=state.job_id)
        return _record_transition(state, Stage.INGESTING)


# ===========================================================================
# Node: ingest  — publishes IngestPendingEvent; graph pauses via interrupt_after.
# ===========================================================================


async def ingest_node(state: JobState, config: RunnableConfig) -> dict[str, Any]:
    """Publish IngestPendingEvent then let the graph pause (interrupt_after).

    Production path: the OrchestratorWorker passes `kafka_producer` in config.
    The graph is called with `interrupt_after=[NODE_INGEST]`, so it pauses here.
    When IngestCompletedEvent arrives, the worker injects real doc refs via
    aupdate_state and resumes with astream(None).

    Stub path (no producer in config): used by unit tests and local dev without
    the scout worker. Produces synthetic doc refs so the graph can run end-to-end.
    """
    attempt = _increment_attempt(state, Stage.INGESTING)
    with tracer.start_as_current_span(
        "orchestrator.ingest",
        attributes={"trendstorm.attempt": attempt, "trendstorm.job_id": state.job_id},
    ):
        logger.info(
            "node_ingest",
            job_id=state.job_id,
            attempt=attempt,
            sources=len(state.sources),
        )

        producer = config.get("configurable", {}).get("kafka_producer")

        if producer is None:
            # Stub: no Kafka producer injected — simulate work for tests.
            await _maybe_fail(Stage.INGESTING)
            await asyncio.sleep(0.1)
            docs = [
                DocumentRef(
                    id=new_id(),
                    source_id=src.id,
                    content_hash=f"stub_{src.id}",
                    char_count=1024,
                )
                for src in state.sources
            ]
            return {
                "attempts": {**state.attempts, Stage.INGESTING: attempt},
                "ingestion": IngestionState(raw_documents=docs),
                **_record_transition(state, Stage.EMBEDDING),
            }

        # Production: publish event and return. The worker's interrupt_after
        # setting pauses the graph here; stage stays INGESTING until resume.
        event = IngestPendingEvent(
            correlation_id=state.observability.correlation_id,
            tenant_id=state.tenant_id,
            job_id=state.job_id,
            source_ids=[src.id for src in state.sources],
            attempt=attempt,
        )
        await producer.send_and_wait(
            Topic.INGEST_PENDING.value,
            value=event.model_dump_json().encode(),
            key=state.job_id.encode(),
        )
        logger.info(
            "ingest_pending_published",
            job_id=state.job_id,
            source_count=len(state.sources),
            attempt=attempt,
        )
        # Stage stays INGESTING; aupdate_state transitions to EMBEDDING on resume.
        return {"attempts": {**state.attempts, Stage.INGESTING: attempt}}


# ===========================================================================
# Node: embed  — Phase 7 dual-mode (Kafka handoff in production, stub in tests)
# ===========================================================================


def _blob_uri_text(blob_uri_raw: str | None) -> str | None:
    """Derive the extracted-text blob URI from the raw blob URI.

    Raw:  s3://bucket/tenant/job/doc/raw.html
    Text: s3://bucket/tenant/job/doc/text.txt
    """
    if not blob_uri_raw:
        return None
    try:
        bucket, key = parse_s3_uri(blob_uri_raw)
        text_key = key.rsplit("/", 1)[0] + "/text.txt"
        return to_s3_uri(bucket, text_key)
    except ValueError:
        return None


async def embed_node(state: JobState, config: RunnableConfig) -> dict[str, Any]:
    """Delegate chunking + embedding to the knowledge worker via Kafka.

    Dual-mode (same pattern as ingest_node):
        Production (kafka_producer in config): publish KnowledgePendingEvent
            and return partial state — graph pauses via interrupt_after=[NODE_EMBED].
        Unit tests (kafka_producer absent): run the stub path so tests complete
            without infrastructure.
    """
    attempt = _increment_attempt(state, Stage.EMBEDDING)
    with tracer.start_as_current_span(
        "orchestrator.embed",
        attributes={"trendstorm.attempt": attempt, "trendstorm.job_id": state.job_id},
    ):
        logger.info(
            "node_embed",
            job_id=state.job_id,
            attempt=attempt,
            docs=len(state.ingestion.raw_documents),
        )

        kafka_producer = config.get("configurable", {}).get("kafka_producer")

        if kafka_producer is not None:
            # Production path: fan out to knowledge worker, then pause.
            from opentelemetry.propagate import inject  # deferred

            doc_refs: list[KnowledgeDocRef] = []
            for doc in state.ingestion.raw_documents:
                uri = _blob_uri_text(doc.blob_uri)
                if not uri:
                    continue
                doc_refs.append(
                    KnowledgeDocRef(
                        document_id=doc.id,
                        blob_uri_text=uri,
                        category_id=state.category_id,
                        source_id=doc.source_id,
                    )
                )

            otel_carrier: dict[str, str] = {}
            inject(otel_carrier)
            event = KnowledgePendingEvent(
                correlation_id=state.observability.correlation_id,
                tenant_id=state.tenant_id,
                traceparent=otel_carrier.get("traceparent"),
                job_id=state.job_id,
                document_refs=doc_refs,
            )
            await kafka_producer.send_and_wait(
                Topic.KNOWLEDGE_PENDING.value,
                value=event.model_dump_json().encode(),
                key=state.job_id.encode(),
            )
            logger.info(
                "knowledge_pending_published",
                job_id=state.job_id,
                n_docs=len(doc_refs),
            )
            # Return partial state — stage stays EMBEDDING; graph pauses here.
            return {
                "attempts": {**state.attempts, Stage.EMBEDDING: attempt},
                **_record_transition(state, Stage.EMBEDDING),
            }

        # Unit-test stub path — no Kafka; run synchronously with synthetic chunks.
        await _maybe_fail(Stage.EMBEDDING)
        await asyncio.sleep(0.1)
        chunks = [
            ChunkRef(id=new_id(), document_id=doc.id)
            for doc in state.ingestion.raw_documents
            for _ in range(3)
        ]
        return {
            "attempts": {**state.attempts, Stage.EMBEDDING: attempt},
            "knowledge": KnowledgeState(
                chunk_refs=chunks,
                embedding_model="stub-embedder-v0",
            ),
            **_record_transition(state, Stage.RETRIEVING),
        }


# ===========================================================================
# Node: retrieve  — Phase 8: thin pass-through. Actual retrieval is delegated
# to the analyst worker (HybridRetriever runs there). This node just marks the
# stage and populates a placeholder RetrievalState so after_retrieve routes
# forward to analyze_node.
# ===========================================================================


async def retrieve_node(state: JobState, config: RunnableConfig) -> dict[str, Any]:
    """Pass-through in production; synthetic chunks in unit-test stub mode.

    Production (kafka_producer present): we are NOT doing retrieval here —
        the analyst worker does hybrid search inside its own pass. We populate
        retrieved_chunk_ids with the chunk IDs from the knowledge stage so
        after_retrieve routes to analyze_node; the analyst worker ignores
        these and computes its own ranked list.
    Stub (no producer): synthetic retrieval of half the chunks, like Phase 4.
    """
    attempt = _increment_attempt(state, Stage.RETRIEVING)
    with tracer.start_as_current_span(
        "orchestrator.retrieve",
        attributes={"trendstorm.attempt": attempt, "trendstorm.job_id": state.job_id},
    ):
        logger.info(
            "node_retrieve",
            job_id=state.job_id,
            attempt=attempt,
            refinement_loop=state.refinement_loops,
        )

        kafka_producer = config.get("configurable", {}).get("kafka_producer")

        if kafka_producer is not None:
            # Production: delegate retrieval to the analyst worker.
            chunk_ids = [c.id for c in state.knowledge.chunk_refs]
            return {
                "attempts": {**state.attempts, Stage.RETRIEVING: attempt},
                "retrieval": RetrievalState(
                    retrieved_chunk_ids=chunk_ids,
                    query="delegated_to_analyst_worker",
                    refinement_count=state.refinement_loops,
                ),
                **_record_transition(state, Stage.ANALYZING),
            }

        # Stub: synthetic retrieval (unit test compatibility).
        await _maybe_fail(Stage.RETRIEVING)
        await asyncio.sleep(0.1)
        retrieved = [
            c.id for c in state.knowledge.chunk_refs[: max(1, len(state.knowledge.chunk_refs) // 2)]
        ]
        return {
            "attempts": {**state.attempts, Stage.RETRIEVING: attempt},
            "retrieval": RetrievalState(
                retrieved_chunk_ids=retrieved,
                query="stub query",
                refinement_count=state.retrieval.refinement_count
                + (1 if state.refinement_loops > 0 else 0),
            ),
            **_record_transition(state, Stage.ANALYZING),
        }


# ===========================================================================
# Node: analyze  — Phase 8 dual-mode (Kafka handoff in production, stub tests)
# ===========================================================================


async def analyze_node(state: JobState, config: RunnableConfig) -> dict[str, Any]:
    """Delegate the analysis pass to the analyst worker via Kafka.

    Dual-mode (same pattern as ingest_node and embed_node):
        Production: publish AnalysisPendingEvent(refinement_loop=state.refinement_loops)
            and return partial state — graph pauses via interrupt_after=[NODE_ANALYZE]
            set at astream() call time by the orchestrator worker.
        Stub: synthetic analysis with calibrated score (unit test path).

    Refinement note: the orchestrator worker handles refinement loops by
    republishing AnalysisPendingEvent directly (bypassing refine_node) when
    a validator fails and the budget has remaining capacity. This node is
    only the entry point for refinement_loop=0.
    """
    attempt = _increment_attempt(state, Stage.ANALYZING)
    with tracer.start_as_current_span(
        "orchestrator.analyze",
        attributes={
            "trendstorm.attempt": attempt,
            "trendstorm.job_id": state.job_id,
            "trendstorm.refinement_loop": state.refinement_loops,
        },
    ):
        logger.info(
            "node_analyze",
            job_id=state.job_id,
            attempt=attempt,
            refinement_loop=state.refinement_loops,
            retrieved=len(state.retrieval.retrieved_chunk_ids),
        )

        kafka_producer = config.get("configurable", {}).get("kafka_producer")

        if kafka_producer is not None:
            from opentelemetry.propagate import inject  # deferred

            otel_carrier: dict[str, str] = {}
            inject(otel_carrier)
            event = AnalysisPendingEvent(
                correlation_id=state.observability.correlation_id,
                tenant_id=state.tenant_id,
                traceparent=otel_carrier.get("traceparent"),
                job_id=state.job_id,
                category_id=state.category_id,
                refinement_loop=state.refinement_loops,
                refinement_notes=None,  # Initial pass — no validator feedback yet
            )
            await kafka_producer.send_and_wait(
                Topic.ANALYSIS_PENDING.value,
                value=event.model_dump_json().encode(),
                key=state.job_id.encode(),
            )
            logger.info(
                "analysis_pending_published",
                job_id=state.job_id,
                refinement_loop=state.refinement_loops,
            )
            # Stage stays ANALYZING; worker aupdate_state injects results on resume.
            return {"attempts": {**state.attempts, Stage.ANALYZING: attempt}}

        # Stub: simulate validation score. First attempt mediocre (0.6),
        # later attempts get better (0.75, 0.9). Exercises the refine loop.
        await _maybe_fail(Stage.ANALYZING)
        await asyncio.sleep(0.1)
        score = 0.6 + 0.15 * state.refinement_loops
        validation_passed = score >= 0.75
        return {
            "attempts": {**state.attempts, Stage.ANALYZING: attempt},
            "analysis": AnalysisState(
                insights_doc_id=new_id(),
                validation_score=score,
                validation_passed=validation_passed,
            ),
            # No transition: after_analyze decides PUBLISH vs REFINE.
        }


# ===========================================================================
# Node: refine  — increments refinement counter before looping back to retrieve.
# ===========================================================================


async def refine_node(state: JobState) -> dict[str, Any]:
    """Increment refinement loop counter and prepare for re-retrieval."""
    with tracer.start_as_current_span("orchestrator.refine"):
        logger.info(
            "node_refine",
            job_id=state.job_id,
            loop=state.refinement_loops + 1,
        )
        return {
            "refinement_loops": state.refinement_loops + 1,
            # Clear retrieved chunks; the loop back to RETRIEVING gets a fresh shot
            "retrieval": RetrievalState(query=state.retrieval.query),
            **_record_transition(state, Stage.RETRIEVING),
        }


# ===========================================================================
# Node: publish  — Phase 9 dual-mode (Kafka handoff in production, stub tests)
# ===========================================================================


async def publish_node(state: JobState, config: RunnableConfig) -> dict[str, Any]:
    """Delegate rendering to the publisher worker via Kafka.

    Dual-mode (same pattern as ingest_node, embed_node, analyze_node):
        Production: publish PublishPendingEvent and return partial state —
            graph pauses via interrupt_after=[NODE_PUBLISH] set at astream()
            call time by the orchestrator worker.
        Stub: synthetic PublishingState with placeholder URIs (unit test path).
    """
    attempt = _increment_attempt(state, Stage.PUBLISHING)
    with tracer.start_as_current_span(
        "orchestrator.publish",
        attributes={"trendstorm.attempt": attempt, "trendstorm.job_id": state.job_id},
    ):
        logger.info("node_publish", job_id=state.job_id, attempt=attempt)

        kafka_producer = config.get("configurable", {}).get("kafka_producer")

        if kafka_producer is not None:
            from opentelemetry.propagate import inject  # deferred

            otel_carrier: dict[str, str] = {}
            inject(otel_carrier)
            analysis_id = state.analysis.insights_doc_id or ""
            event = PublishPendingEvent(
                correlation_id=state.observability.correlation_id,
                tenant_id=state.tenant_id,
                traceparent=otel_carrier.get("traceparent"),
                job_id=state.job_id,
                analysis_id=analysis_id,
                category_id=state.category_id,
            )
            await kafka_producer.send_and_wait(
                Topic.PUBLISH_PENDING.value,
                value=event.model_dump_json().encode(),
                key=state.job_id.encode(),
            )
            logger.info(
                "publish_pending_published",
                job_id=state.job_id,
                analysis_id=analysis_id,
            )
            # Stage stays PUBLISHING; orchestrator worker injects results on resume.
            return {"attempts": {**state.attempts, Stage.PUBLISHING: attempt}}

        # Stub: advance to MEMORY_CONSOLIDATION without a real publisher.
        await _maybe_fail(Stage.PUBLISHING)
        await asyncio.sleep(0.1)
        return {
            "attempts": {**state.attempts, Stage.PUBLISHING: attempt},
            "publishing": PublishingState(
                report_doc_id=new_id(),
                report_blob_uri=f"s3://trendstorm-reports/{state.job_id}/report.md",
            ),
            **_record_transition(state, Stage.MEMORY_CONSOLIDATION),
        }


# ===========================================================================
# Node: review_gate  — HITL pass-through or pause for human review (Phase 13.5)
# ===========================================================================


async def review_gate_node(state: JobState, config: RunnableConfig) -> dict[str, Any]:
    """Gate analysis to human review or pass it directly to publishing.

    Decision logic:
        1. skip_hitl_gate=True  → always pass-through (post-review-resolution path).
        2. kafka_producer absent → stub path (unit tests), always pass-through.
        3. HITL mode "off"      → pass-through.
        4. HITL mode "always"   → gate.
        5. HITL mode "flagged_only" → gate if:
               - validator_score < hitl_validator_threshold, OR
               - refinement_loops >= max_refinement_loops (budget exhausted), OR
               - cost_so_far_usd > hitl_cost_threshold_usd (if configured).

    Pass-through: returns {stage: PUBLISHING}.
    Gate: creates a ReviewRequest in Mongo, publishes ReviewRequestedEvent,
    returns {stage: AWAITING_REVIEW, pending_review_id: review_id}.
    The orchestrator resumes via _handle_review_resolved when the reviewer
    decides (or the timeout sweeper fires).
    """
    with tracer.start_as_current_span("orchestrator.review_gate"):
        # --- 1. skip_hitl_gate: cleared after a review resolves to avoid re-gating.
        if state.skip_hitl_gate:
            logger.info("review_gate.skip_hitl", job_id=state.job_id)
            return {"stage": Stage.PUBLISHING, "skip_hitl_gate": False}

        kafka_producer = config.get("configurable", {}).get("kafka_producer")

        # --- 2. Stub path (no Kafka / unit tests)
        if kafka_producer is None:
            return {"stage": Stage.PUBLISHING}

        # --- 3. Load tenant settings (deferred imports keep module-level clean).
        from trendstorm.domain.tenant_settings.models import (
            DEFAULT_TENANT_SETTINGS,
            HitlMode,
        )

        settings_repo = config.get("configurable", {}).get("tenant_settings_repo")
        if settings_repo is not None:
            ts = await settings_repo.get_for_tenant(state.tenant_id)
            tenant_settings = ts if ts is not None else DEFAULT_TENANT_SETTINGS
        else:
            tenant_settings = DEFAULT_TENANT_SETTINGS

        if tenant_settings.hitl_mode == HitlMode.OFF:
            return {"stage": Stage.PUBLISHING}

        # --- 4/5. Determine whether to flag.
        score = state.analysis.validation_score
        loops = state.refinement_loops
        from trendstorm.agents.state import MAX_REFINEMENT_LOOPS

        budget_exhausted = loops >= MAX_REFINEMENT_LOOPS

        should_gate = (
            tenant_settings.hitl_mode == HitlMode.ALWAYS
            or score < tenant_settings.hitl_validator_threshold
            or budget_exhausted
        )

        if not should_gate:
            return {"stage": Stage.PUBLISHING}

        # --- Gate: create review record and publish event.
        from datetime import UTC, datetime, timedelta

        from trendstorm_shared import FlaggingReason

        from trendstorm.domain.reviews.models import ReviewRequest
        from trendstorm.orchestration.events import ReviewRequestedEvent

        timeout_hours = tenant_settings.hitl_timeout_hours
        timeout_at = datetime.now(UTC) + timedelta(hours=timeout_hours)

        # Determine why this review was flagged.
        if tenant_settings.hitl_mode.value == "always":
            flagging_reason = FlaggingReason.ALWAYS_MODE
        elif budget_exhausted:
            flagging_reason = FlaggingReason.REFINEMENT_BUDGET_EXHAUSTED
        else:
            flagging_reason = FlaggingReason.LOW_VALIDATOR_SCORE

        review = ReviewRequest(
            tenant_id=state.tenant_id,
            job_id=state.job_id,
            analysis_id=state.analysis.insights_doc_id or "",
            stage_under_review=state.stage.value,
            timeout_at=timeout_at,
            sla_seconds=timeout_hours * 3600,
            validator_score=score,
            refinement_loops_used=loops,
            cost_usd_so_far_cents=0,  # TODO: wire cost ledger into node context
            flagging_reason=flagging_reason,
        )

        review_repo = config.get("configurable", {}).get("review_repo")
        if review_repo is not None:
            await review_repo.insert(review)
            logger.info(
                "review_gate.created",
                job_id=state.job_id,
                review_id=review.id,
                score=score,
                mode=tenant_settings.hitl_mode.value,
            )

        from opentelemetry.propagate import inject

        otel_carrier: dict[str, str] = {}
        inject(otel_carrier)

        event = ReviewRequestedEvent(
            correlation_id=state.observability.correlation_id,
            tenant_id=state.tenant_id,
            traceparent=otel_carrier.get("traceparent"),
            job_id=state.job_id,
            review_id=review.id,
            analysis_id=review.analysis_id,
            validator_score=score,
            refinement_loops=loops,
            timeout_at=timeout_at,
        )
        await kafka_producer.send_and_wait(
            Topic.REVIEW_REQUESTED.value,
            value=event.model_dump_json().encode(),
            key=state.job_id.encode(),
        )
        logger.info(
            "review_gate.published",
            job_id=state.job_id,
            review_id=review.id,
            timeout_at=timeout_at.isoformat(),
        )
        return {
            "stage": Stage.AWAITING_REVIEW,
            "pending_review_id": review.id,
        }


# ===========================================================================
# Node: memory_consolidation  — Phase 15.5: episodic + semantic memory write.
# ===========================================================================


async def memory_consolidation_node(state: JobState, config: RunnableConfig) -> dict[str, Any]:
    """Publish MemoryPendingEvent for the memory-consolidation worker.

    Dual-mode (same pattern as all other handoff nodes):
        Production (kafka_producer present): publish MemoryPendingEvent and
            return partial state — graph pauses via interrupt_after=[NODE_MEMORY_CONSOLIDATION].
        Stub (no producer): advance to COMPLETED directly (tests skip memory I/O).

    Memory failure is non-blocking: if the budget is exhausted, after_memory_consolidation
    routes to NODE_END (COMPLETED) regardless. The report is already published.
    """
    attempt = _increment_attempt(state, Stage.MEMORY_CONSOLIDATION)
    with tracer.start_as_current_span(
        "orchestrator.memory_consolidation",
        attributes={"trendstorm.attempt": attempt, "trendstorm.job_id": state.job_id},
    ):
        logger.info("node_memory_consolidation", job_id=state.job_id, attempt=attempt)

        kafka_producer = config.get("configurable", {}).get("kafka_producer")

        if kafka_producer is not None:
            from opentelemetry.propagate import inject  # deferred

            otel_carrier: dict[str, str] = {}
            inject(otel_carrier)
            analysis_id = state.analysis.insights_doc_id or ""
            report_id = state.publishing.report_doc_id
            event = MemoryPendingEvent(
                correlation_id=state.observability.correlation_id,
                tenant_id=state.tenant_id,
                traceparent=otel_carrier.get("traceparent"),
                job_id=state.job_id,
                analysis_id=analysis_id,
                category_id=state.category_id,
                report_id=report_id,
                attempt=attempt,
            )
            await kafka_producer.send_and_wait(
                Topic.MEMORY_PENDING.value,
                value=event.model_dump_json().encode(),
                key=state.job_id.encode(),
            )
            logger.info(
                "memory_pending_published",
                job_id=state.job_id,
                analysis_id=analysis_id,
            )
            # Stage stays MEMORY_CONSOLIDATION; orchestrator worker injects results.
            return {
                "attempts": {**state.attempts, Stage.MEMORY_CONSOLIDATION: attempt},
                **_record_transition(state, Stage.MEMORY_CONSOLIDATION),
            }

        # Stub: advance to COMPLETED without real memory I/O.
        await asyncio.sleep(0.05)
        return {
            "attempts": {**state.attempts, Stage.MEMORY_CONSOLIDATION: attempt},
            "memory": MemoryConsolidationState(
                episodic_memory_id=new_id(),
                semantic_memory_ids=[],
            ),
            "stage": Stage.COMPLETED,
        }


# ===========================================================================
# Node: fail  — records the failure on state and transitions to FAILED.
# ===========================================================================


async def fail_node(state: JobState) -> dict[str, Any]:
    """Terminal failure node. Marks the job FAILED with last-error context."""
    with tracer.start_as_current_span("orchestrator.fail"):
        last_err = state.errors[-1] if state.errors else None
        logger.warning(
            "node_fail",
            job_id=state.job_id,
            stage=state.stage.value,
            last_error_code=last_err.code if last_err else None,
        )
        return {"stage": Stage.FAILED}
