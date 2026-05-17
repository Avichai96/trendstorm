"""Integration tests requiring the full Docker stack to be up.

Run:
    make up                   # bring up infrastructure
    uv run pytest tests/integration -m integration

These tests exercise the real app lifespan with real Mongo/Kafka/Redis.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from trendstorm.api.main import create_app
from trendstorm.shared.config import get_settings
from trendstorm.shared.ids import new_id

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()


@pytest.fixture
async def app_client():
    """Start the app via lifespan and yield an httpx AsyncClient.

    Uses ASGITransport for in-process HTTP — no real network sockets, but
    full middleware/lifespan pipeline.
    """
    app = create_app()
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        yield client


async def test_liveness(app_client: AsyncClient) -> None:
    r = await app_client.get("/health/live")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_liveness_returns_correlation_header(app_client: AsyncClient) -> None:
    r = await app_client.get("/health/live")
    assert "x-correlation-id" in r.headers
    assert len(r.headers["x-correlation-id"]) == 26  # ULID length


async def test_correlation_id_is_echoed(app_client: AsyncClient) -> None:
    cid = new_id()
    r = await app_client.get("/health/live", headers={"X-Correlation-ID": cid})
    assert r.headers["x-correlation-id"] == cid


async def test_invalid_correlation_id_replaced(app_client: AsyncClient) -> None:
    r = await app_client.get("/health/live", headers={"X-Correlation-ID": "garbage"})
    # We don't trust unvalidated IDs; a fresh ULID replaces it.
    assert r.headers["x-correlation-id"] != "garbage"
    assert len(r.headers["x-correlation-id"]) == 26


async def test_readiness_ok_with_stack(app_client: AsyncClient) -> None:
    r = await app_client.get("/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    components = {c["name"]: c["healthy"] for c in body["components"]}
    assert components == {
        "mongo": True,
        "redis": True,
        "kafka": True,
        "blob": True,
        "vector_store": True,
    }


async def test_tenant_required_for_jobs(app_client: AsyncClient) -> None:
    r = await app_client.post(
        "/v1/jobs",
        json={"category_id": new_id()},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["code"] == "missing_tenant"


async def test_create_job_stub(app_client: AsyncClient) -> None:
    tenant = new_id()
    r = await app_client.post(
        "/v1/jobs",
        headers={"X-Tenant-ID": tenant},
        json={"category_id": new_id(), "note": "test"},
    )
    assert r.status_code == 202
    body = r.json()
    assert len(body["job_id"]) == 26
    assert body["status"] == "pending"
    assert body["stream_url"].startswith("/v1/jobs/")


async def test_validation_error_envelope(app_client: AsyncClient) -> None:
    tenant = new_id()
    r = await app_client.post(
        "/v1/jobs",
        headers={"X-Tenant-ID": tenant},
        json={},  # missing required category_id
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "validation_error"
    assert "errors" in body["error"]["context"]
    assert body["correlation_id"] is not None
