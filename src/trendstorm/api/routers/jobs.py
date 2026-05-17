"""Jobs router — real implementation as of Phase 4.

POST /v1/jobs        Create a job, persist, publish to Kafka.
GET  /v1/jobs/{id}   Fetch a single job.
GET  /v1/jobs        List the tenant's jobs with cursor pagination.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sse_starlette.sse import EventSourceResponse

from trendstorm.api.deps import MongoDep, PubSubDep, SettingsDep, StreamStoreDep
from trendstorm.domain.jobs.models import Job
from trendstorm.infrastructure.mongo.repositories import MongoJobRepository
from trendstorm.services.job_service import JobService
from trendstorm.services.streaming.sse import sse_event_generator
from trendstorm.shared.errors import NotFoundError
from trendstorm.shared.ids import is_valid_id
from trendstorm.shared.logging import get_logger
from trendstorm.shared.types import JobStatus

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])


# --- Schemas ----------------------------------------------------------------

class CreateJobRequest(BaseModel):
    """Request body for job creation."""

    model_config = ConfigDict(extra="forbid")

    category_id: str = Field(..., description="ID of an existing trend category")
    source_ids: list[str] = Field(default_factory=list, description="Source IDs to ingest")
    note: str | None = Field(default=None, max_length=500)


class JobMetricsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    documents_ingested: int = 0
    chunks_created: int = 0
    chunks_retrieved: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    duration_seconds: float | None = None


class JobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: JobStatus
    category_id: str
    source_ids: list[str]
    note: str | None = None
    analysis_id: str | None = None
    report_id: str | None = None
    metrics: JobMetricsResponse
    failure_code: str | None = None
    failure_message: str | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    stream_url: str | None = None


class JobAcceptedResponse(BaseModel):
    """202 response — work happens async; client polls or streams."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: JobStatus
    stream_url: str
    created_at: datetime


class JobListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jobs: list[JobResponse]
    next_cursor: str | None = None


# --- Dependency wiring ------------------------------------------------------

def get_job_service(mongo: MongoDep) -> JobService:
    """Construct a JobService per request. Cheap (no I/O)."""
    from trendstorm.infrastructure.mongo.repositories.cost_ledger_repository import (
        MongoCostLedgerRepository,
    )
    from trendstorm.infrastructure.mongo.repositories.outbox_repository import MongoOutboxRepository
    from trendstorm.infrastructure.mongo.repositories.tenant_repository import MongoTenantRepository
    from trendstorm.services.billing.quota_service import QuotaService

    jobs_repo = MongoJobRepository(mongo)
    outbox_repo = MongoOutboxRepository(mongo)
    ledger_repo = MongoCostLedgerRepository(mongo)
    tenant_repo = MongoTenantRepository(mongo)
    quota_svc = QuotaService(ledger=ledger_repo, tenants=tenant_repo)
    return JobService(jobs=jobs_repo, outbox=outbox_repo, mongo=mongo, quota=quota_svc)


JobServiceDep = Annotated[JobService, Depends(get_job_service)]


def _job_to_response(job_obj: Job) -> JobResponse:
    """Map domain Job to API JobResponse, attaching stream_url."""
    return JobResponse(
        id=job_obj.id,
        status=job_obj.status,
        category_id=job_obj.category_id,
        source_ids=job_obj.source_ids,
        note=job_obj.note,
        analysis_id=job_obj.analysis_id,
        report_id=job_obj.report_id,
        metrics=JobMetricsResponse(**job_obj.metrics.model_dump()),
        failure_code=job_obj.failure_code,
        failure_message=job_obj.failure_message,
        created_at=job_obj.created_at,
        updated_at=job_obj.updated_at,
        completed_at=job_obj.completed_at,
        stream_url=f"/v1/jobs/{job_obj.id}/stream",
    )


# --- Endpoints --------------------------------------------------------------

@router.post(
    "",
    response_model=JobAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a new trend analysis job",
)
async def create_job(
    request: Request,
    body: CreateJobRequest,
    service: JobServiceDep,
) -> JobAcceptedResponse:
    """Create a new trend analysis job.

    Returns 202 Accepted with the job id and stream URL. Actual analysis
    happens asynchronously in worker processes.
    """
    tenant_id = request.state.tenant_id
    job = await service.create_job(
        tenant_id=tenant_id,
        category_id=body.category_id,
        source_ids=body.source_ids,
        note=body.note,
    )
    return JobAcceptedResponse(
        job_id=job.id,
        status=job.status,
        stream_url=f"/v1/jobs/{job.id}/stream",
        created_at=job.created_at,
    )


@router.get(
    "/{job_id}",
    response_model=JobResponse,
    summary="Fetch a single job",
)
async def get_job(
    request: Request,
    mongo: MongoDep,
    job_id: Annotated[str, Path(min_length=26, max_length=26)],
) -> JobResponse:
    """Fetch a job by id. 404 if not found in the current tenant."""
    if not is_valid_id(job_id):
        raise NotFoundError(f"Job {job_id} not found")
    repo = MongoJobRepository(mongo)
    job = await repo.get(request.state.tenant_id, job_id)
    if job is None:
        raise NotFoundError(f"Job {job_id} not found")
    return _job_to_response(job)


@router.get(
    "",
    response_model=JobListResponse,
    summary="List the tenant's jobs",
)
async def list_jobs(
    request: Request,
    mongo: MongoDep,
    status_filter: Annotated[JobStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(min_length=26, max_length=26)] = None,
) -> JobListResponse:
    """List jobs for the current tenant, newest first, cursor-paginated."""
    repo = MongoJobRepository(mongo)
    jobs, next_cursor = await repo.list_by_tenant(
        request.state.tenant_id,
        status=status_filter,
        limit=limit,
        cursor=cursor,
    )
    return JobListResponse(
        jobs=[_job_to_response(j) for j in jobs],
        next_cursor=next_cursor,
    )


@router.get(
    "/{job_id}/stream",
    summary="Stream job events via SSE",
    response_class=EventSourceResponse,
)
async def stream_job(
    request: Request,
    mongo: MongoDep,
    stream_store: StreamStoreDep,
    pubsub: PubSubDep,
    settings: SettingsDep,
    job_id: Annotated[str, Path(min_length=26, max_length=26)],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> EventSourceResponse:
    """Stream real-time job events as Server-Sent Events.

    Connect with `EventSource('/v1/jobs/{id}/stream')`. The stream:
        - Replays history from the last-seen event (via `Last-Event-ID` header).
        - Tails live events via Redis Pub/Sub.
        - Emits heartbeat comments every {sse.heartbeat_seconds} seconds.
        - Closes when a terminal event (REPORT_READY or JOB_FAILED) arrives.

    404 if the job doesn't exist or doesn't belong to this tenant.
    """
    if not is_valid_id(job_id):
        raise NotFoundError(f"Job {job_id} not found")

    # Verify tenant ownership before streaming.
    repo = MongoJobRepository(mongo)
    job = await repo.get(request.state.tenant_id, job_id)
    if job is None:
        raise NotFoundError(f"Job {job_id} not found")

    # Parse Last-Event-ID if present (client reconnects with this to resume).
    resume_from: int = 0
    if last_event_id:
        try:
            resume_from = int(last_event_id)
        except ValueError:
            resume_from = 0

    logger.info(
        "sse_stream_connected",
        job_id=job_id,
        tenant_id=request.state.tenant_id,
        resume_from=resume_from,
    )

    async def _generate() -> AsyncIterator[str]:
        async for chunk in sse_event_generator(
            job_id,
            stream_store=stream_store,
            pubsub=pubsub,
            settings=settings.sse,
            last_event_id=resume_from,
        ):
            yield chunk

    return EventSourceResponse(_generate())
