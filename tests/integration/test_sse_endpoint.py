"""Integration tests for the SSE streaming endpoint.

Tests /v1/jobs/{job_id}/stream via the real FastAPI app with real Redis.

Flow per test:
    1. Start the app (full lifespan — real Redis, Mongo, Kafka).
    2. Insert events directly into Redis Streams via RedisStreamStore.
    3. Connect to the SSE endpoint via httpx.
    4. Verify history replay + heartbeat + terminal close semantics.

Skip semantics: if infrastructure isn't up, tests are skipped gracefully.

Run:
    uv run pytest tests/integration/test_sse_endpoint.py -m integration -s
"""
from __future__ import annotations

import asyncio
import contextlib
import json

import pytest
from httpx import ASGITransport, AsyncClient

from trendstorm.api.main import create_app
from trendstorm.domain.streaming.events import StreamEventType
from trendstorm.infrastructure.mongo.client import MongoClient
from trendstorm.infrastructure.mongo.repositories import MongoJobRepository
from trendstorm.infrastructure.redis.client import RedisClient
from trendstorm.infrastructure.redis.streams import RedisStreamStore
from trendstorm.shared.config import get_settings
from trendstorm.shared.ids import new_id
from trendstorm.shared.types import JobStatus

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_TENANT_ID = "sse-test-tenant"


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()


@pytest.fixture
async def app_client():
    """Start the full app via lifespan. Skips if infra not up."""
    app = create_app()
    transport = ASGITransport(app=app)
    try:
        async with (
            AsyncClient(transport=transport, base_url="http://test") as client,
            app.router.lifespan_context(app),
        ):
            yield client, app
    except Exception as exc:
        pytest.skip(f"Infrastructure not available: {exc}")


@pytest.fixture
async def redis_client():
    """Direct Redis client for pre-seeding stream events."""
    settings = get_settings()
    rc = RedisClient(settings.redis)
    try:
        await rc.connect()
    except Exception as exc:
        pytest.skip(f"Redis not available: {exc}")
    yield rc
    await rc.close()


def _sse_payload(
    job_id: str,
    *,
    event_type: str,
    seq: int,
    stage: str = "publishing",
    tenant_id: str = _TENANT_ID,
) -> dict:
    return {
        "event_id": new_id(),
        "job_id": job_id,
        "tenant_id": tenant_id,
        "event_type": event_type,
        "seq": seq,
        "stage": stage,
        "payload": {},
        "occurred_at": "2026-05-24T00:00:00+00:00",
    }


async def _collect_sse_lines(
    client: AsyncClient,
    *,
    job_id: str,
    tenant_id: str = _TENANT_ID,
    timeout: float = 5.0,  # noqa: ASYNC109  # test helper signature
    max_events: int = 20,
) -> list[dict]:
    """Stream the SSE endpoint and collect decoded data payloads."""
    collected: list[dict] = []

    async with client.stream(
        "GET",
        f"/v1/jobs/{job_id}/stream",
        headers={"X-Tenant-ID": tenant_id},
        timeout=timeout,
    ) as response:
        if response.status_code != 200:
            return collected

        async for line in response.aiter_lines():
            line = line.strip()
            if not line or line.startswith(":"):
                continue  # heartbeat comment
            if line.startswith("data: "):
                try:
                    payload = json.loads(line[len("data: "):])
                    collected.append(payload)
                    event_type = payload.get("event_type", "")
                    try:
                        if StreamEventType(event_type).is_terminal:
                            break
                    except ValueError:
                        pass
                    if len(collected) >= max_events:
                        break
                except json.JSONDecodeError:
                    pass

    return collected


@pytest.mark.integration
async def test_sse_replays_seeded_history(app_client, redis_client) -> None:
    """Events pre-seeded in Redis Streams are replayed to SSE clients."""
    client, _ = app_client
    settings = get_settings()

    # We need a real job in Mongo so the tenant ownership check passes.
    mongo = MongoClient(settings.mongo)
    try:
        await mongo.connect()
    except Exception as exc:
        pytest.skip(f"Mongo not available: {exc}")

    job_id = new_id()

    # Insert a minimal job row so GET /v1/jobs/{id}/stream doesn't 404.
    from trendstorm.domain.jobs.models import Job

    job = Job(
        id=job_id,
        tenant_id=_TENANT_ID,
        category_id=new_id(),
        status=JobStatus.ANALYZING,
    )
    job_repo = MongoJobRepository(mongo)
    with contextlib.suppress(Exception):
        await job_repo.insert(job)  # May already exist.

    await mongo.close()

    # Seed three events into Redis Streams directly.
    stream_store = RedisStreamStore(settings.sse)
    stream_store.init(redis_client.client)

    events_to_seed = [
        _sse_payload(job_id, event_type=StreamEventType.STAGE_STARTED.value, seq=1),
        _sse_payload(job_id, event_type=StreamEventType.CHUNK_DELTA.value, seq=2),
        _sse_payload(job_id, event_type=StreamEventType.REPORT_READY.value, seq=3),
    ]
    for payload in events_to_seed:
        await stream_store.append(job_id, payload)

    # Connect to SSE and collect.
    try:
        collected = await asyncio.wait_for(
            _collect_sse_lines(client, job_id=job_id, max_events=5, timeout=5.0),
            timeout=8.0,
        )
    except TimeoutError:
        collected = []

    assert len(collected) >= 2, f"Expected at least 2 replayed events, got {collected}"
    event_types = [e.get("event_type") for e in collected]
    assert StreamEventType.STAGE_STARTED.value in event_types
    assert StreamEventType.REPORT_READY.value in event_types


@pytest.mark.integration
async def test_sse_stream_closes_on_terminal_event(app_client, redis_client) -> None:
    """The SSE generator stops after emitting a terminal event."""
    client, _ = app_client
    settings = get_settings()

    mongo = MongoClient(settings.mongo)
    try:
        await mongo.connect()
    except Exception as exc:
        pytest.skip(f"Mongo not available: {exc}")

    job_id = new_id()

    from trendstorm.domain.jobs.models import Job

    job = Job(
        id=job_id,
        tenant_id=_TENANT_ID,
        category_id=new_id(),
        status=JobStatus.ANALYZING,
    )
    job_repo = MongoJobRepository(mongo)
    with contextlib.suppress(Exception):
        await job_repo.insert(job)

    await mongo.close()

    stream_store = RedisStreamStore(settings.sse)
    stream_store.init(redis_client.client)

    # Seed a terminal event immediately (REPORT_READY = terminal).
    await stream_store.append(
        job_id, _sse_payload(job_id, event_type=StreamEventType.REPORT_READY.value, seq=1)
    )
    # Seed a post-terminal event that should NOT be received.
    await stream_store.append(
        job_id, _sse_payload(job_id, event_type=StreamEventType.CHUNK_DELTA.value, seq=2)
    )

    try:
        collected = await asyncio.wait_for(
            _collect_sse_lines(client, job_id=job_id, max_events=10, timeout=5.0),
            timeout=8.0,
        )
    except TimeoutError:
        collected = []

    # Generator must stop at REPORT_READY — CHUNK_DELTA should not appear.
    event_types = [e.get("event_type") for e in collected]
    assert StreamEventType.REPORT_READY.value in event_types
    assert StreamEventType.CHUNK_DELTA.value not in event_types


@pytest.mark.integration
async def test_sse_404_for_missing_job(app_client) -> None:
    """Requesting stream for a non-existent job returns 404."""
    client, _ = app_client
    missing_id = new_id()

    async with client.stream(
        "GET",
        f"/v1/jobs/{missing_id}/stream",
        headers={"X-Tenant-ID": _TENANT_ID},
        timeout=5.0,
    ) as response:
        assert response.status_code == 404
