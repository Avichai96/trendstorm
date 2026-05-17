"""Job service — use-case orchestration for jobs.

This is the THIN layer between the API router and the domain/infrastructure.

Phase 12: job creation uses the outbox pattern.

What `create_job` does:
    1. Build a Job domain object.
    2. Build an OutboxEntry for the JobRequestedEvent.
    3. Write BOTH to Mongo inside a single Mongo transaction.
    4. Return the Job to the router.

The outbox-relay worker later picks up the OutboxEntry and publishes to Kafka.
If Kafka is temporarily down, the outbox accumulates entries and drains once
Kafka recovers — no jobs are lost.

Previously (Phase 4): job was persisted then Kafka published directly. A Kafka
failure after job persist left the job stuck in PENDING with no event to drive
it. The outbox pattern closes this window.

The retry failure mode is now: job+outbox commit succeeds (job visible in API),
Kafka publish eventually happens (relay retries). The window between "user sees
job PENDING" and "job actually starts processing" depends on relay poll interval
(default 500ms). This is acceptable.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.propagate import inject

from trendstorm.domain.jobs.models import Job
from trendstorm.domain.outbox.models import OutboxEntry
from trendstorm.orchestration.events import JobRequestedEvent
from trendstorm.orchestration.topics import Topic
from trendstorm.shared.errors import BusinessRuleError, DatabaseError
from trendstorm.shared.logging import get_correlation_id, get_logger

if TYPE_CHECKING:
    from trendstorm.domain.jobs.repository import JobRepository
    from trendstorm.domain.outbox.repository import OutboxRepository
    from trendstorm.infrastructure.mongo.client import MongoClient
    from trendstorm.services.billing.quota_service import QuotaService

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)


class JobService:
    """Service for job use cases."""

    def __init__(
        self,
        *,
        jobs: JobRepository,
        outbox: OutboxRepository,
        mongo: MongoClient,
        quota: QuotaService | None = None,
    ) -> None:
        self._jobs = jobs
        self._outbox = outbox
        self._mongo = mongo
        self._quota = quota

    async def create_job(
        self,
        *,
        tenant_id: str,
        category_id: str,
        source_ids: list[str],
        note: str | None = None,
    ) -> Job:
        """Create a job and atomically enqueue a JobRequestedEvent via the outbox."""
        with tracer.start_as_current_span(
            "service.create_job",
            attributes={
                "trendstorm.tenant_id": tenant_id,
                "trendstorm.category_id": category_id,
                "trendstorm.source_count": len(source_ids),
            },
        ):
            # Pre-flight quota check: refuse before any writes.
            if self._quota is not None:
                status = await self._quota.check(tenant_id)
                if not status.allowed:
                    raise BusinessRuleError(
                        status.reason or "Quota exceeded",
                        code="quota_exceeded",
                        context={
                            "monthly_spend_usd": status.monthly_spend_usd_micro / 1_000_000,
                            "jobs_this_month": status.jobs_this_month,
                        },
                    )

            job = Job(
                tenant_id=tenant_id,
                category_id=category_id,
                source_ids=source_ids,
                note=note,
            )

            # Inject traceparent so the consumer's span continues this trace.
            carrier: dict[str, str] = {}
            inject(carrier)

            event = JobRequestedEvent(
                correlation_id=get_correlation_id() or "unknown",
                tenant_id=tenant_id,
                job_id=job.id,
                category_id=category_id,
                source_ids=source_ids,
                note=note,
                traceparent=carrier.get("traceparent"),
            )

            outbox_entry = OutboxEntry(
                tenant_id=tenant_id,
                topic=Topic.JOBS_REQUESTED.value,
                key=job.id,
                payload=json.loads(event.model_dump_json()),
            )

            # Atomic: both succeed or both fail.
            # If this transaction fails the user gets a clean error and retries.
            # If it succeeds, the relay publishes the outbox entry to Kafka within
            # poll_interval (default 500ms).
            try:
                async with await self._mongo.client.start_session() as session, session.start_transaction():
                    await self._jobs.insert(job, session=session)
                    await self._outbox.insert(outbox_entry, session=session)
            except Exception as e:
                logger.exception("job_create_transaction_failed", job_id=job.id)
                raise DatabaseError(
                    "Job creation failed",
                    context={"job_id": job.id, "error": str(e)},
                ) from e

            logger.info(
                "job_created",
                job_id=job.id,
                outbox_entry_id=outbox_entry.id,
            )
            return job
