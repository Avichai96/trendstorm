"""Orchestrator worker.

Consumes `trendstorm.jobs.requested.v1` events from Kafka, drives a
LangGraph workflow per job, and persists the result to MongoDB.

How it runs:
    python -m trendstorm.orchestration.workers.orchestrator_worker

Or in Docker:
    docker run trendstorm-worker python -m trendstorm.orchestration.workers.orchestrator_worker

One process = one consumer in the `trendstorm.orchestrator` consumer group.
N replicas = N consumers, splitting partitions of `jobs.requested.v1`.

What happens per message:
    1. Parse JobRequestedEvent.
    2. Idempotency check (job_id-based).
    3. Load Job from Mongo (must exist — API created it).
    4. Build initial JobState from the event.
    5. ainvoke the graph; LangGraph checkpoints to Mongo throughout.
    6. On terminal state, update the Job's status in Mongo.

Resume semantics:
    If this worker crashes mid-graph, Kafka redelivers the message. The new
    consumer's idempotency check sees in_progress -> skips. Wait — that
    means we'd never resume!

    Resolution: The orchestrator worker SHOULD process duplicates by
    resuming. We override `_idempotency_key` to return None for the
    orchestrator (LangGraph's checkpointer provides resume safety,
    making per-message idempotency unnecessary AND undesirable here).

    For downstream workers (scout, knowledge, etc.) we DO use idempotency
    because they don't have a checkpointer of their own.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from opentelemetry.propagate import inject

from trendstorm.agents.orchestrator.checkpointer import MongoCheckpointer
from trendstorm.agents.orchestrator.edges import (
    NODE_ANALYZE,
    NODE_EMBED,
    NODE_INGEST,
    NODE_MEMORY_CONSOLIDATION,
    NODE_PUBLISH,
)
from trendstorm.agents.orchestrator.graph import build_orchestrator_graph
from trendstorm.agents.stages import Stage
from trendstorm.agents.state import (
    AnalysisState,
    ChunkRef,
    DocumentRef,
    IngestionState,
    JobState,
    KnowledgeState,
    MemoryConsolidationState,
    PublishingState,
    SourceRef,
)
from trendstorm.infrastructure.kafka.consumer import BaseConsumer
from trendstorm.infrastructure.kafka.producer import KafkaProducerClient
from trendstorm.infrastructure.mongo.client import MongoClient
from trendstorm.infrastructure.mongo.repositories import (
    IdempotencyRepository,
    MongoAnalysisRepository,
    MongoJobRepository,
    MongoReviewRepository,
)
from trendstorm.orchestration.events import (
    AnalysisCompletedEvent,
    AnalysisPendingEvent,
    EventEnvelope,
    IngestCompletedEvent,
    JobRequestedEvent,
    KnowledgeCompletedEvent,
    MemoryCompletedEvent,
    PublishCompletedEvent,
    ReviewResolvedEvent,
)
from trendstorm.orchestration.topics import ConsumerGroup, Topic
from trendstorm.domain.streaming.events import StreamEvent, StreamEventType
from trendstorm.services.streaming.emit import emit_stream_event
from trendstorm.shared.config import AnalysisSettings, KafkaSettings, get_settings
from trendstorm.shared.errors import NotFoundError
from trendstorm.shared.ids import new_id
from trendstorm.shared.logging import configure_logging, get_logger
from trendstorm.shared.metrics.registry import METRICS
from trendstorm.shared.tracing import configure_tracing
from trendstorm.shared.types import JobStatus

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig
    from langgraph.graph.state import CompiledStateGraph


logger = get_logger(__name__)


# ===========================================================================
# Mapping: Stage  ->  JobStatus
#
# Internal stages compress into user-facing statuses. Refinement loops, for
# example, are invisible to users — they just see "analyzing" twice in
# rapid succession, or a single longer "analyzing" period.
# ===========================================================================
_STAGE_TO_STATUS: dict[Stage, JobStatus] = {
    Stage.PENDING: JobStatus.PENDING,
    Stage.INGESTING: JobStatus.INGESTING,
    Stage.EMBEDDING: JobStatus.EMBEDDING,
    Stage.RETRIEVING: JobStatus.RETRIEVING,
    Stage.ANALYZING: JobStatus.ANALYZING,
    Stage.AWAITING_REVIEW: JobStatus.AWAITING_REVIEW,
    Stage.PUBLISHING: JobStatus.PUBLISHING,
    Stage.MEMORY_CONSOLIDATION: JobStatus.PUBLISHING,  # user-visible as "publishing" (internal detail)
    Stage.COMPLETED: JobStatus.COMPLETED,
    Stage.FAILED: JobStatus.FAILED,
    Stage.CANCELLED: JobStatus.CANCELLED,
    Stage.REJECTED: JobStatus.REJECTED,
}


class OrchestratorWorker(BaseConsumer):
    """Consumes job-requested events and runs them through the graph."""

    def __init__(
        self,
        *,
        settings: KafkaSettings,
        graph: CompiledStateGraph[Any, Any, Any],
        job_repo: MongoJobRepository,
        analysis_repo: MongoAnalysisRepository,
        review_repo: MongoReviewRepository,
        idempotency: IdempotencyRepository,
        producer: KafkaProducerClient,
        analysis_settings: AnalysisSettings,
    ) -> None:
        super().__init__(
            topics=[
                Topic.JOBS_REQUESTED,
                Topic.INGEST_COMPLETED,
                Topic.KNOWLEDGE_COMPLETED,
                Topic.ANALYSIS_COMPLETED,
                Topic.PUBLISH_COMPLETED,
                Topic.REVIEW_RESOLVED,
                Topic.MEMORY_COMPLETED,
            ],
            group_id=ConsumerGroup.ORCHESTRATOR.value,
            settings=settings,
            idempotency=idempotency,
            producer=producer,
            worker_name="orchestrator",
        )
        self._graph = graph
        self._jobs = job_repo
        self._analyses = analysis_repo
        self._reviews = review_repo
        self._analysis_settings = analysis_settings

    def _idempotency_key(self, event: EventEnvelope) -> str | None:
        """Disabled for orchestrator: LangGraph's checkpointer handles resume.

        See module docstring for the rationale.
        """
        return None

    async def handle(self, event: EventEnvelope) -> None:
        """Dispatch to the correct handler based on event type."""
        if isinstance(event, JobRequestedEvent):
            await self._handle_job_requested(event)
        elif isinstance(event, IngestCompletedEvent):
            await self._handle_ingest_completed(event)
        elif isinstance(event, KnowledgeCompletedEvent):
            await self._handle_knowledge_completed(event)
        elif isinstance(event, AnalysisCompletedEvent):
            await self._handle_analysis_completed(event)
        elif isinstance(event, PublishCompletedEvent):
            await self._handle_publish_completed(event)
        elif isinstance(event, ReviewResolvedEvent):
            await self._handle_review_resolved(event)
        elif isinstance(event, MemoryCompletedEvent):
            await self._handle_memory_completed(event)
        else:
            logger.warning(
                "orchestrator_unexpected_event",
                event_type=getattr(event, "event_type", "unknown"),
            )

    def _record_handle_metrics(
        self, event: EventEnvelope, status: str, elapsed: float
    ) -> None:
        METRICS.orchestrator_events.labels(
            tenant_id=event.tenant_id,
            event_type=getattr(event, "event_type", "unknown"),
            status=status,
        ).inc()

    async def _handle_job_requested(self, event: JobRequestedEvent) -> None:
        """Start the graph for a new job. Pauses after ingest_node."""
        job = await self._jobs.get(event.tenant_id, event.job_id)
        if job is None:
            raise NotFoundError(
                f"Job {event.job_id} not found",
                context={"tenant_id": event.tenant_id},
            )

        state = JobState.initial(
            tenant_id=event.tenant_id,
            category_id=event.category_id,
            sources=[
                SourceRef(id=sid, type="http", label=sid)
                for sid in event.source_ids
            ],
            correlation_id=event.correlation_id,
        )
        state = state.model_copy(update={"job_id": event.job_id})

        config: RunnableConfig = {
            "configurable": {
                "thread_id": event.job_id,
                # Injected into ingest_node so it can publish IngestPendingEvent.
                "kafka_producer": self._producer.producer,
            }
        }

        final_state: JobState | None = None
        try:
            async for step in self._graph.astream(
                state,
                config=config,
                interrupt_after=[NODE_INGEST],  # pause; scout handles ingestion
            ):
                for node_name, _update in step.items():
                    if node_name == "__interrupt__":
                        continue
                    logger.info("graph_step", job_id=event.job_id, node=node_name)

            snapshot = await self._graph.aget_state(config)
            final_state = JobState.model_validate(snapshot.values)
        except Exception:
            logger.exception("graph_start_failed", job_id=event.job_id)
            await self._jobs.update_status(
                event.tenant_id, event.job_id, JobStatus.FAILED,
                failure_code="graph_error",
                failure_message="Graph failed on startup",
            )
            raise

        if final_state:
            new_status = _STAGE_TO_STATUS.get(final_state.stage, JobStatus.FAILED)
            await self._jobs.update_status(
                event.tenant_id, event.job_id, new_status,
                failure_code=None, failure_message=None,
            )
            logger.info(
                "graph_paused_awaiting_scout",
                job_id=event.job_id,
                stage=final_state.stage.value,
            )

    async def _handle_ingest_completed(self, event: IngestCompletedEvent) -> None:
        """Resume the graph after the scout worker finishes ingestion."""
        config: RunnableConfig = {"configurable": {"thread_id": event.job_id}}

        # Guard: skip if the job is already in a terminal state.
        snapshot = await self._graph.aget_state(config)
        if not snapshot.values:
            logger.warning("no_checkpoint_for_job", job_id=event.job_id)
            return
        current = JobState.model_validate(snapshot.values)
        if current.stage.is_terminal:
            logger.info(
                "job_already_terminal",
                job_id=event.job_id,
                stage=current.stage.value,
            )
            return

        # Convert IngestDocRef → DocumentRef (in-memory agent state model).
        doc_refs = [
            DocumentRef(
                id=r.id,
                source_id=r.source_id,
                content_hash=r.content_hash,
                blob_uri=r.blob_uri_raw,
                char_count=r.char_count,
            )
            for r in event.document_refs
        ]
        ingestion = IngestionState(
            raw_documents=doc_refs,
            failed_source_ids=event.failed_source_ids,
        )

        state_update: dict[str, Any] = {"ingestion": ingestion}
        if doc_refs:
            # Advance stage so after_ingest routes to embed_node.
            # Bypassing _record_transition here is intentional — this is an
            # out-of-band state injection from the scout worker, not a node return.
            state_update["stage"] = Stage.EMBEDDING

        # Inject ingestion results as if ingest_node returned them.
        await self._graph.aupdate_state(config, state_update, as_node=NODE_INGEST)

        # Resume the graph; pause again at embed_node so the knowledge worker
        # can do its work before the graph continues to retrieve_node.
        final_state: JobState | None = None
        try:
            async for step in self._graph.astream(
                None,
                config=config,
                interrupt_after=[NODE_EMBED],
            ):
                for node_name, _update in step.items():
                    if node_name == "__interrupt__":
                        continue
                    logger.info("graph_step", job_id=event.job_id, node=node_name)

            snapshot = await self._graph.aget_state(config)
            final_state = JobState.model_validate(snapshot.values)
        except Exception:
            logger.exception("graph_resume_failed", job_id=event.job_id)
            await self._jobs.update_status(
                event.tenant_id, event.job_id, JobStatus.FAILED,
                failure_code="graph_resume_error",
                failure_message="Graph resume failed after ingest",
            )
            raise

        if final_state:
            new_status = _STAGE_TO_STATUS.get(final_state.stage, JobStatus.FAILED)
            await self._jobs.update_status(
                event.tenant_id, event.job_id, new_status,
                failure_code=None if new_status != JobStatus.FAILED else "graph_failed",
                failure_message=(
                    None if new_status != JobStatus.FAILED
                    else f"Graph terminated at stage {final_state.stage.value}"
                ),
            )
            logger.info(
                "graph_paused_awaiting_knowledge",
                job_id=event.job_id,
                stage=final_state.stage.value,
            )


    async def _handle_knowledge_completed(self, event: KnowledgeCompletedEvent) -> None:
        """Resume the graph after the knowledge worker finishes chunking+embedding."""
        config: RunnableConfig = {"configurable": {"thread_id": event.job_id}}

        snapshot = await self._graph.aget_state(config)
        if not snapshot.values:
            logger.warning("no_checkpoint_for_job", job_id=event.job_id)
            return
        current = JobState.model_validate(snapshot.values)
        if current.stage.is_terminal:
            logger.info(
                "job_already_terminal",
                job_id=event.job_id,
                stage=current.stage.value,
            )
            return

        # Build ChunkRef list from per-document results.
        # One synthetic ref per successful document — Phase 8 retrieve_node
        # does its own vector search anyway; this just signals embedding is done.
        chunk_refs = [
            ChunkRef(id=new_id(), document_id=result.document_id)
            for result in event.document_results
            if not result.skipped and result.n_chunks > 0
        ]

        state_update: dict[str, Any] = {
            "knowledge": KnowledgeState(chunk_refs=chunk_refs),
        }
        if chunk_refs:
            state_update["stage"] = Stage.RETRIEVING

        # Inject knowledge results as if embed_node returned them.
        await self._graph.aupdate_state(config, state_update, as_node=NODE_EMBED)

        final_state: JobState | None = None
        try:
            # Pause at NODE_ANALYZE so the analyst worker can do retrieve+analyze+validate.
            async for step in self._graph.astream(
                None,
                config=config,
                interrupt_after=[NODE_ANALYZE],
            ):
                for node_name, _update in step.items():
                    if node_name == "__interrupt__":
                        continue
                    logger.info("graph_step", job_id=event.job_id, node=node_name)

            snapshot = await self._graph.aget_state(config)
            final_state = JobState.model_validate(snapshot.values)
        except Exception:
            logger.exception("graph_resume_failed_after_knowledge", job_id=event.job_id)
            await self._jobs.update_status(
                event.tenant_id, event.job_id, JobStatus.FAILED,
                failure_code="graph_resume_error",
                failure_message="Graph resume failed after knowledge embedding",
            )
            raise

        if final_state:
            new_status = _STAGE_TO_STATUS.get(final_state.stage, JobStatus.FAILED)
            await self._jobs.update_status(
                event.tenant_id, event.job_id, new_status,
                failure_code=None if new_status != JobStatus.FAILED else "graph_failed",
                failure_message=(
                    None if new_status != JobStatus.FAILED
                    else f"Graph terminated at stage {final_state.stage.value}"
                ),
            )
            logger.info(
                "graph_paused_awaiting_analyst",
                job_id=event.job_id,
                stage=final_state.stage.value,
            )

    async def _handle_analysis_completed(self, event: AnalysisCompletedEvent) -> None:
        """Resume after the analyst worker finishes one analysis pass.

        Three paths:
            1. success=False (permanent failure)  → fail the job.
            2. passed OR budget exhausted         → advance to publishing.
            3. failed AND budget remaining        → republish a refined
                AnalysisPendingEvent with refinement_loop+1 and the
                validator notes loaded from the persisted Analysis.
        """
        config: RunnableConfig = {"configurable": {"thread_id": event.job_id}}

        snapshot = await self._graph.aget_state(config)
        if not snapshot.values:
            logger.warning("no_checkpoint_for_job", job_id=event.job_id)
            return
        current = JobState.model_validate(snapshot.values)
        if current.stage.is_terminal:
            logger.info(
                "job_already_terminal",
                job_id=event.job_id,
                stage=current.stage.value,
            )
            return

        # ---- Catastrophic / permanent analyst failure ---------------------
        if not event.success:
            logger.error(
                "analyst_reported_failure",
                job_id=event.job_id,
                refinement_loop=event.refinement_loop,
                error_code=event.error_code,
                error_message=event.error_message,
            )
            await self._jobs.update_status(
                event.tenant_id, event.job_id, JobStatus.FAILED,
                failure_code=event.error_code or "analyst_failed",
                failure_message=event.error_message or "Analyst reported failure",
            )
            return

        max_loops = self._analysis_settings.max_refinement_loops
        analysis_state = AnalysisState(
            insights_doc_id=event.analysis_id,
            validation_score=event.score,
            validation_passed=event.passed,
        )

        # ---- Pass OR budget exhausted: advance to publishing --------------
        if event.passed or event.refinement_loop >= max_loops:
            state_update: dict[str, Any] = {
                "analysis": analysis_state,
                "stage": Stage.PUBLISHING,
                "refinement_loops": event.refinement_loop,
            }
            await self._graph.aupdate_state(config, state_update, as_node=NODE_ANALYZE)
            await self._resume_to_publish(event, config)
            return

        # ---- Refine: budget remaining and validator did not pass ----------
        analysis = None
        if event.analysis_id:
            analysis = await self._analyses.get(event.tenant_id, event.analysis_id)
        validator_notes = (analysis.validator_notes if analysis else None) or (
            "Prior analysis did not meet the validator threshold; address grounding "
            "and faithfulness in this attempt."
        )

        # Update state so refinement_loops reflects the NEW attempt index.
        refine_state: dict[str, Any] = {
            "analysis": analysis_state,
            "refinement_loops": event.refinement_loop + 1,
        }
        await self._graph.aupdate_state(config, refine_state, as_node=NODE_ANALYZE)

        await self._publish_refinement_request(
            event=event,
            current=current,
            new_loop=event.refinement_loop + 1,
            refinement_notes=validator_notes,
        )

        logger.info(
            "analyst_refinement_requested",
            job_id=event.job_id,
            new_refinement_loop=event.refinement_loop + 1,
            prior_score=event.score,
        )

    async def _publish_refinement_request(
        self,
        *,
        event: AnalysisCompletedEvent,
        current: JobState,
        new_loop: int,
        refinement_notes: str,
    ) -> None:
        """Send a new AnalysisPendingEvent for the next refinement attempt."""
        otel_carrier: dict[str, str] = {}
        inject(otel_carrier)

        pending = AnalysisPendingEvent(
            correlation_id=event.correlation_id,
            tenant_id=event.tenant_id,
            traceparent=otel_carrier.get("traceparent"),
            job_id=event.job_id,
            category_id=current.category_id,
            refinement_loop=new_loop,
            refinement_notes=refinement_notes,
        )
        await self._producer.producer.send_and_wait(
            Topic.ANALYSIS_PENDING.value,
            value=pending.model_dump_json().encode(),
            key=event.job_id.encode(),
        )

    async def _resume_to_publish(
        self,
        event: AnalysisCompletedEvent,
        config: RunnableConfig,
    ) -> None:
        """Stream the graph into publish_node, then pause — publisher worker handles render."""
        # Inject kafka_producer so publish_node uses the production path.
        pub_config: RunnableConfig = {
            "configurable": {
                **config.get("configurable", {}),
                "kafka_producer": self._producer.producer,
            }
        }
        final_state: JobState | None = None
        try:
            async for step in self._graph.astream(
                None,
                config=pub_config,
                interrupt_after=[NODE_PUBLISH],
            ):
                for node_name, _update in step.items():
                    if node_name == "__interrupt__":
                        continue
                    logger.info("graph_step", job_id=event.job_id, node=node_name)

            snapshot = await self._graph.aget_state(pub_config)
            final_state = JobState.model_validate(snapshot.values)
        except Exception:
            logger.exception(
                "graph_resume_failed_after_analysis",
                job_id=event.job_id,
            )
            await self._jobs.update_status(
                event.tenant_id, event.job_id, JobStatus.FAILED,
                failure_code="graph_resume_error",
                failure_message="Graph resume failed after analysis completed",
            )
            raise

        if final_state:
            new_status = _STAGE_TO_STATUS.get(final_state.stage, JobStatus.FAILED)
            await self._jobs.update_status(
                event.tenant_id, event.job_id, new_status,
                failure_code=None if new_status != JobStatus.FAILED else "graph_failed",
                failure_message=(
                    None if new_status != JobStatus.FAILED
                    else f"Graph terminated at stage {final_state.stage.value}"
                ),
            )
            # Notify SSE subscribers if the job is now waiting for human review.
            if final_state.stage == Stage.AWAITING_REVIEW and final_state.pending_review_id:
                await emit_stream_event(
                    StreamEvent(
                        job_id=event.job_id,
                        tenant_id=event.tenant_id,
                        event_type=StreamEventType.REVIEW_REQUIRED,
                        payload={
                            "review_id": final_state.pending_review_id,
                            "analysis_id": final_state.analysis.insights_doc_id,
                            "validator_score": final_state.analysis.validation_score,
                        },
                    ),
                    producer=self._producer,
                    correlation_id=final_state.observability.correlation_id,
                )
            logger.info(
                "graph_paused_awaiting_publisher",
                job_id=event.job_id,
                stage=final_state.stage.value,
            )

    async def _handle_review_resolved(self, event: ReviewResolvedEvent) -> None:
        """Resume or terminate the job after a human review decision.

        Decision semantics:
            approve          → advance stage to PUBLISHING, set skip_hitl_gate=True,
                               resume graph from NODE_REVIEW_GATE → publish.
            reject           → update job status to REJECTED; no graph resume.
            request_refinement → publish a new AnalysisPendingEvent with the
                               reviewer's comment as refinement_notes; no astream.
        """
        from trendstorm.agents.orchestrator.edges import NODE_REVIEW_GATE
        from trendstorm.domain.reviews.models import ReviewDecision

        config: RunnableConfig = {"configurable": {"thread_id": event.job_id}}

        snapshot = await self._graph.aget_state(config)
        if not snapshot.values:
            logger.warning("no_checkpoint_for_job", job_id=event.job_id)
            return
        current = JobState.model_validate(snapshot.values)
        if current.stage.is_terminal:
            logger.info("job_already_terminal", job_id=event.job_id, stage=current.stage.value)
            return

        try:
            decision = ReviewDecision(event.decision)
        except ValueError:
            logger.error("review_resolved.unknown_decision",
                         job_id=event.job_id, decision=event.decision)
            return

        # Mark review resolved in Mongo.
        await self._reviews.resolve(
            event.tenant_id,
            event.review_id,
            decision=decision,
            comment=event.comment,
            reviewer_id=event.resolved_by,
        )

        if decision == ReviewDecision.REJECT:
            await self._jobs.update_status(
                event.tenant_id, event.job_id, JobStatus.REJECTED,
                failure_code="review_rejected",
                failure_message=event.comment or "Analysis rejected by reviewer.",
            )
            await emit_stream_event(
                StreamEvent(
                    job_id=event.job_id,
                    tenant_id=event.tenant_id,
                    event_type=StreamEventType.JOB_REJECTED,
                    payload={"review_id": event.review_id, "comment": event.comment},
                ),
                producer=self._producer,
                correlation_id=current.observability.correlation_id,
            )
            logger.info("review_resolved.rejected", job_id=event.job_id,
                        review_id=event.review_id, resolved_by=event.resolved_by)
            return

        if decision == ReviewDecision.APPROVE:
            # Advance stage; skip_hitl_gate prevents re-gating on graph resume.
            await self._graph.aupdate_state(
                config,
                {"stage": Stage.PUBLISHING, "pending_review_id": None, "skip_hitl_gate": True},
                as_node=NODE_REVIEW_GATE,
            )
            await emit_stream_event(
                StreamEvent(
                    job_id=event.job_id,
                    tenant_id=event.tenant_id,
                    event_type=StreamEventType.REVIEW_RESOLVED,
                    payload={"review_id": event.review_id, "decision": "approve"},
                ),
                producer=self._producer,
                correlation_id=current.observability.correlation_id,
            )
            await self._resume_to_publish_after_review(event, config)
            logger.info("review_resolved.approved", job_id=event.job_id,
                        review_id=event.review_id, resolved_by=event.resolved_by)
            return

        # decision == ReviewDecision.REQUEST_REFINEMENT
        new_loop = current.refinement_loops + 1
        refinement_notes = event.comment or (
            "Prior analysis was flagged for refinement by a human reviewer. "
            "Improve grounding, specificity, and faithfulness."
        )
        await self._graph.aupdate_state(
            config,
            {
                "stage": Stage.ANALYZING,
                "pending_review_id": None,
                "skip_hitl_gate": True,
                "review_decision_comment": event.comment,
                "refinement_loops": new_loop,
            },
            as_node=NODE_REVIEW_GATE,
        )
        await self._publish_refinement_request_from_review(
            event=event,
            current=current,
            new_loop=new_loop,
            refinement_notes=refinement_notes,
        )
        await self._jobs.update_status(
            event.tenant_id, event.job_id, JobStatus.ANALYZING,
            failure_code=None, failure_message=None,
        )
        logger.info("review_resolved.refinement_requested", job_id=event.job_id,
                    review_id=event.review_id, new_loop=new_loop)

    async def _publish_refinement_request_from_review(
        self,
        *,
        event: ReviewResolvedEvent,
        current: JobState,
        new_loop: int,
        refinement_notes: str,
    ) -> None:
        """Publish AnalysisPendingEvent for a reviewer-requested refinement."""
        otel_carrier: dict[str, str] = {}
        inject(otel_carrier)
        pending = AnalysisPendingEvent(
            correlation_id=current.observability.correlation_id,
            tenant_id=event.tenant_id,
            traceparent=otel_carrier.get("traceparent"),
            job_id=event.job_id,
            category_id=current.category_id,
            refinement_loop=new_loop,
            refinement_notes=refinement_notes,
        )
        await self._producer.producer.send_and_wait(
            Topic.ANALYSIS_PENDING.value,
            value=pending.model_dump_json().encode(),
            key=event.job_id.encode(),
        )

    async def _resume_to_publish_after_review(
        self,
        event: ReviewResolvedEvent,
        config: RunnableConfig,
    ) -> None:
        """Resume graph from NODE_REVIEW_GATE → NODE_PUBLISH after review approval."""
        pub_config: RunnableConfig = {
            "configurable": {
                **config.get("configurable", {}),
                "kafka_producer": self._producer.producer,
            }
        }
        final_state: JobState | None = None
        try:
            async for step in self._graph.astream(
                None, config=pub_config, interrupt_after=[NODE_PUBLISH],
            ):
                for node_name, _update in step.items():
                    if node_name == "__interrupt__":
                        continue
                    logger.info("graph_step", job_id=event.job_id, node=node_name)

            snapshot = await self._graph.aget_state(pub_config)
            final_state = JobState.model_validate(snapshot.values)
        except Exception:
            logger.exception("graph_resume_failed_after_review_approval", job_id=event.job_id)
            await self._jobs.update_status(
                event.tenant_id, event.job_id, JobStatus.FAILED,
                failure_code="graph_resume_error",
                failure_message="Graph resume failed after review approval",
            )
            raise

        if final_state:
            new_status = _STAGE_TO_STATUS.get(final_state.stage, JobStatus.FAILED)
            await self._jobs.update_status(
                event.tenant_id, event.job_id, new_status,
                failure_code=None, failure_message=None,
            )

    async def _handle_publish_completed(self, event: PublishCompletedEvent) -> None:
        """Resume the graph after the publisher worker finishes rendering.

        On success: inject PublishingState + MEMORY_CONSOLIDATION →
            resume with interrupt_after=[NODE_MEMORY_CONSOLIDATION] →
            memory_consolidation_node publishes MemoryPendingEvent → graph pauses.
        On failure: update job status to FAILED.
        """
        config: RunnableConfig = {"configurable": {"thread_id": event.job_id}}

        snapshot = await self._graph.aget_state(config)
        if not snapshot.values:
            logger.warning("no_checkpoint_for_job", job_id=event.job_id)
            return
        current = JobState.model_validate(snapshot.values)
        if current.stage.is_terminal:
            logger.info(
                "job_already_terminal",
                job_id=event.job_id,
                stage=current.stage.value,
            )
            return

        if not event.success:
            logger.error(
                "publisher_reported_failure",
                job_id=event.job_id,
                error_code=event.error_code,
                error_message=event.error_message,
            )
            await self._jobs.update_status(
                event.tenant_id, event.job_id, JobStatus.FAILED,
                failure_code=event.error_code or "publisher_failed",
                failure_message=event.error_message or "Publisher reported failure",
            )
            return

        # Inject the report IDs as PublishingState; advance to MEMORY_CONSOLIDATION.
        report_uri = (
            f"s3://trendstorm-reports/{event.job_id}"
            f"/{event.markdown_report_id}/report.md"
        ) if event.markdown_report_id else ""

        await self._graph.aupdate_state(
            config,
            {
                "stage": Stage.MEMORY_CONSOLIDATION,
                "publishing": PublishingState(
                    report_doc_id=event.markdown_report_id or "",
                    report_blob_uri=report_uri,
                ),
            },
            as_node=NODE_PUBLISH,
        )

        # Resume — after_publish sees report_doc_id → NODE_MEMORY_CONSOLIDATION.
        # memory_consolidation_node publishes MemoryPendingEvent and pauses.
        kafka_config: dict[str, Any] = {}
        if hasattr(self, "_producer"):
            kafka_config["kafka_producer"] = self._producer
        mem_config: RunnableConfig = {"configurable": {"thread_id": event.job_id, **kafka_config}}

        try:
            async for step in self._graph.astream(
                None,
                config=mem_config,
                interrupt_after=[NODE_MEMORY_CONSOLIDATION],
            ):
                for node_name, _update in step.items():
                    if node_name == "__interrupt__":
                        continue
                    logger.info("graph_step", job_id=event.job_id, node=node_name)
        except Exception:
            logger.exception("graph_resume_failed_after_publish", job_id=event.job_id)
            await self._jobs.update_status(
                event.tenant_id, event.job_id, JobStatus.FAILED,
                failure_code="graph_resume_error",
                failure_message="Graph resume failed after publish completed",
            )
            raise

        logger.info(
            "publish_completed_memory_pending",
            job_id=event.job_id,
            markdown_report_id=event.markdown_report_id,
        )

    async def _handle_memory_completed(self, event: MemoryCompletedEvent) -> None:
        """Resume the graph after the memory-consolidation worker finishes.

        On success or partial-failure: inject MemoryConsolidationState + COMPLETED
            → resume → after_memory_consolidation → END.
        Memory failure is non-blocking — job reaches COMPLETED regardless.
        """
        config: RunnableConfig = {"configurable": {"thread_id": event.job_id}}

        snapshot = await self._graph.aget_state(config)
        if not snapshot.values:
            logger.warning("no_checkpoint_for_job_memory", job_id=event.job_id)
            return
        current = JobState.model_validate(snapshot.values)
        if current.stage.is_terminal:
            logger.info(
                "job_already_terminal_memory",
                job_id=event.job_id,
                stage=current.stage.value,
            )
            return

        await self._graph.aupdate_state(
            config,
            {
                "stage": Stage.COMPLETED,
                "memory": MemoryConsolidationState(
                    episodic_memory_id=event.episodic_memory_id,
                    semantic_memory_ids=event.semantic_memory_ids,
                ),
            },
            as_node=NODE_MEMORY_CONSOLIDATION,
        )

        final_state: JobState | None = None
        try:
            async for step in self._graph.astream(None, config=config):
                for node_name, _update in step.items():
                    if node_name == "__interrupt__":
                        continue
                    logger.info("graph_step", job_id=event.job_id, node=node_name)

            snapshot = await self._graph.aget_state(config)
            final_state = JobState.model_validate(snapshot.values)
        except Exception:
            logger.exception("graph_resume_failed_after_memory", job_id=event.job_id)
            await self._jobs.update_status(
                event.tenant_id, event.job_id, JobStatus.FAILED,
                failure_code="graph_resume_error_memory",
                failure_message="Graph resume failed after memory consolidation",
            )
            raise

        if final_state:
            new_status = _STAGE_TO_STATUS.get(final_state.stage, JobStatus.COMPLETED)
            await self._jobs.update_status(
                event.tenant_id, event.job_id, new_status,
                failure_code=None if new_status != JobStatus.FAILED else "graph_failed",
                failure_message=(
                    None if new_status != JobStatus.FAILED
                    else f"Graph terminated at stage {final_state.stage.value}"
                ),
            )
            logger.info(
                "job_completed_with_memory",
                job_id=event.job_id,
                stage=final_state.stage.value,
                status=new_status.value,
                episodic_memory_id=event.episodic_memory_id,
                n_semantic_memories=len(event.semantic_memory_ids),
            )


# ===========================================================================
# Process entrypoint
# ===========================================================================

async def run_worker() -> None:
    """Start the orchestrator worker process, blocking until shutdown."""
    settings = get_settings()
    configure_logging()
    configure_tracing(service_name="trendstorm-orchestrator")
    logger.info("orchestrator_worker_booting")

    # Build infrastructure
    mongo = MongoClient(settings.mongo)
    producer = KafkaProducerClient(settings.kafka)
    await asyncio.gather(mongo.connect(), producer.start())

    job_repo = MongoJobRepository(mongo)
    analysis_repo = MongoAnalysisRepository(mongo)
    idem = IdempotencyRepository(mongo)

    checkpointer = MongoCheckpointer(settings.mongo)
    await checkpointer.start()

    graph = build_orchestrator_graph(checkpointer.saver)

    worker = OrchestratorWorker(
        settings=settings.kafka,
        graph=graph,
        job_repo=job_repo,
        analysis_repo=analysis_repo,
        idempotency=idem,
        producer=producer,
        analysis_settings=settings.analysis,
    )

    await worker.start()
    worker.install_signal_handlers()

    try:
        await worker.run()
    finally:
        logger.info("orchestrator_worker_shutting_down")
        await worker.stop()
        await checkpointer.close()
        await producer.stop()
        await mongo.close()


def main() -> None:
    """Run the worker synchronously — entry point for `python -m`."""
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_worker())


if __name__ == "__main__":
    main()
