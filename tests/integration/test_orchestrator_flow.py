"""Integration test: API -> Kafka -> Worker -> Mongo update.

Requires `make up` (Mongo + Kafka + Redis) AND a running orchestrator
worker subprocess (which we spawn inline).

Flow:
    1. Spawn the orchestrator worker as a background asyncio task.
    2. Hit POST /v1/jobs through ASGI to create a job.
    3. Wait (with a timeout) until the worker drives the job to COMPLETED.
    4. Verify Mongo reflects the terminal status.
    5. Cancel the worker.

Skip semantics:
    If the Docker stack isn't running, this test is skipped (not failed).
    The smoke test in scripts/smoke_test.py is the gate that verifies the
    stack itself.
"""
from __future__ import annotations

import asyncio
import contextlib

import pytest
from httpx import ASGITransport, AsyncClient

from trendstorm.agents.orchestrator.checkpointer import MongoCheckpointer
from trendstorm.agents.orchestrator.graph import build_orchestrator_graph
from trendstorm.api.main import create_app
from trendstorm.infrastructure.kafka.producer import KafkaProducerClient
from trendstorm.infrastructure.mongo.client import MongoClient
from trendstorm.infrastructure.mongo.repositories import (
    IdempotencyRepository,
    MongoAnalysisRepository,
    MongoJobRepository,
)
from trendstorm.orchestration.workers.orchestrator_worker import OrchestratorWorker
from trendstorm.shared.config import get_settings
from trendstorm.shared.ids import new_id
from trendstorm.shared.types import JobStatus

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()


@pytest.fixture
async def worker_task():
    """Spawn the orchestrator worker as a background task.

    Yields the (mongo, producer) clients so the test can inspect Mongo.
    Cleans up everything on teardown.
    """
    settings = get_settings()

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
    task = asyncio.create_task(worker.run())

    try:
        yield mongo, producer
    finally:
        await worker.stop()
        with contextlib.suppress(asyncio.CancelledError):
            task.cancel()
            await task
        await checkpointer.close()
        await producer.stop()
        await mongo.close()


@pytest.fixture
async def app_client():
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client, app.router.lifespan_context(app):
        yield client


async def _wait_for_status(
    mongo: MongoClient,
    tenant_id: str,
    job_id: str,
    target_statuses: set[JobStatus],
    timeout_seconds: float = 30.0,
) -> JobStatus:
    """Poll Mongo until job reaches one of the target statuses."""
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    repo = MongoJobRepository(mongo)
    last_status: JobStatus | None = None
    while asyncio.get_event_loop().time() < deadline:
        job = await repo.get(tenant_id, job_id)
        if job is not None:
            last_status = job.status
            if job.status in target_statuses:
                return job.status
        await asyncio.sleep(0.5)
    raise AssertionError(
        f"Job {job_id} never reached {target_statuses}; "
        f"last status={last_status}"
    )


@pytest.mark.skip(
    reason=(
        "Phase 6+ async handoff: this fixture spawns ONLY the orchestrator "
        "worker, so the graph pauses at INGESTING awaiting the scout worker. "
        "Driving to COMPLETED requires scout + knowledge + analyst workers, "
        "which is exactly what tests/integration/test_analyst_e2e.py does as "
        "the canonical full-pipeline regression. See "
        "test_create_job_advances_to_ingesting_then_waits_for_scout below for "
        "the orchestrator-only handoff assertion."
    )
)
async def test_create_job_drives_to_completed(
    app_client: AsyncClient,
    worker_task,
) -> None:
    """End-to-end: HTTP POST -> Kafka -> worker -> Mongo COMPLETED."""
    mongo, _ = worker_task
    tenant_id = new_id()

    response = await app_client.post(
        "/v1/jobs",
        headers={"X-Tenant-ID": tenant_id},
        json={
            "category_id": new_id(),
            "source_ids": [new_id(), new_id()],
            "note": "integration test",
        },
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    final_status = await _wait_for_status(
        mongo, tenant_id, job_id,
        target_statuses={JobStatus.COMPLETED, JobStatus.FAILED},
        timeout_seconds=30.0,
    )
    assert final_status == JobStatus.COMPLETED, (
        f"Expected COMPLETED but got {final_status}"
    )


async def test_create_job_advances_to_ingesting_then_waits_for_scout(
    app_client: AsyncClient,
    worker_task,
) -> None:
    """Orchestrator-only handoff regression.

    With ONLY the orchestrator worker running (no scout), the graph must:
        1. Receive the JobRequestedEvent and start the LangGraph workflow.
        2. Run init_job + ingest_node (which publishes IngestPendingEvent).
        3. Pause at the NODE_INGEST interrupt awaiting scout.
        4. Update the Mongo job status to INGESTING.

    This verifies the async-resume handoff pattern (CLAUDE.md rule 21) works
    end-to-end through the message bus, without needing scout / knowledge /
    analyst workers running. The full-pipeline COMPLETED path is covered by
    test_analyst_e2e.py.
    """
    mongo, _ = worker_task
    tenant_id = new_id()

    response = await app_client.post(
        "/v1/jobs",
        headers={"X-Tenant-ID": tenant_id},
        json={
            "category_id": new_id(),
            "source_ids": [new_id(), new_id()],
            "note": "orchestrator handoff regression",
        },
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    # Graph pauses at NODE_INGEST awaiting scout — job.status becomes INGESTING.
    status = await _wait_for_status(
        mongo, tenant_id, job_id,
        target_statuses={JobStatus.INGESTING},
        timeout_seconds=15.0,
    )
    assert status == JobStatus.INGESTING

    # Job MUST NOT have advanced past INGESTING (no scout running).
    # Give the orchestrator a chance to (incorrectly) progress; verify it didn't.
    await asyncio.sleep(2.0)
    repo = MongoJobRepository(mongo)
    job = await repo.get(tenant_id, job_id)
    assert job is not None
    assert job.status == JobStatus.INGESTING, (
        f"Job advanced past INGESTING without scout running: {job.status}"
    )


async def test_create_job_returns_immediately(app_client: AsyncClient) -> None:
    """The POST must return 202 well under a second — work is async."""
    import time as _t
    tenant_id = new_id()
    start = _t.perf_counter()
    response = await app_client.post(
        "/v1/jobs",
        headers={"X-Tenant-ID": tenant_id},
        json={"category_id": new_id(), "source_ids": [new_id()]},
    )
    elapsed = _t.perf_counter() - start
    assert response.status_code == 202
    assert elapsed < 2.0, f"POST took {elapsed:.2f}s — should be near-instant"
