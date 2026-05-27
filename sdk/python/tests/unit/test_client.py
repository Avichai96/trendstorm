"""Unit tests for TrendStormClient — every endpoint mocked with respx."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
import respx
import httpx

from trendstorm_sdk import TrendStormClient, NotFound, RateLimited


BASE = "https://api.trendstorm.test"
API_KEY = "ts_test_UnitTestApiKey1234567890"

_NOW = datetime.now(timezone.utc).isoformat()

_CAT = {
    "id": "01CAT567890123456789012345",
    "name": "AI Safety",
    "description": None,
    "keywords": ["alignment"],
    "archived": False,
    "created_at": _NOW,
    "updated_at": _NOW,
}

_SOURCE = {
    "id": "01SRC567890123456789012345",
    "category_id": _CAT["id"],
    "url": "https://example.com/feed.rss",
    "label": None,
    "type": "rss",
    "enabled": True,
    "last_fetch_at": None,
    "last_fetch_status": None,
    "last_fetch_error": None,
    "created_at": _NOW,
}

_JOB_ACCEPTED = {
    "job_id": "01JOB567890123456789012345",
    "status": "pending",
    "stream_url": "/v1/jobs/01JOB567890123456789012345/stream",
    "created_at": _NOW,
}

_JOB = {
    "id": "01JOB567890123456789012345",
    "status": "completed",
    "category_id": _CAT["id"],
    "source_ids": [_SOURCE["id"]],
    "note": None,
    "analysis_id": None,
    "report_id": None,
    "metrics": {
        "documents_ingested": 3,
        "chunks_created": 42,
        "chunks_retrieved": 10,
        "llm_input_tokens": 1000,
        "llm_output_tokens": 200,
        "duration_seconds": 12.5,
    },
    "failure_code": None,
    "failure_message": None,
    "created_at": _NOW,
    "updated_at": _NOW,
    "completed_at": _NOW,
    "stream_url": "/v1/jobs/01JOB567890123456789012345/stream",
}


@pytest.mark.unit
class TestCategoriesEndpoints:
    @respx.mock
    async def test_create_category(self) -> None:
        respx.post(f"{BASE}/v1/categories").mock(return_value=httpx.Response(201, json=_CAT))
        async with TrendStormClient(api_key=API_KEY, base_url=BASE, max_retries=0) as ts:
            cat = await ts.categories.create(name="AI Safety", keywords=["alignment"])
        assert cat.id == _CAT["id"]
        assert cat.name == "AI Safety"

    @respx.mock
    async def test_get_category(self) -> None:
        respx.get(f"{BASE}/v1/categories/{_CAT['id']}").mock(return_value=httpx.Response(200, json=_CAT))
        async with TrendStormClient(api_key=API_KEY, base_url=BASE, max_retries=0) as ts:
            cat = await ts.categories.get(_CAT["id"])
        assert cat.id == _CAT["id"]

    @respx.mock
    async def test_list_categories(self) -> None:
        payload = {"categories": [_CAT], "next_cursor": None}
        respx.get(f"{BASE}/v1/categories").mock(return_value=httpx.Response(200, json=payload))
        async with TrendStormClient(api_key=API_KEY, base_url=BASE, max_retries=0) as ts:
            resp = await ts.categories.list()
        assert len(resp.categories) == 1

    @respx.mock
    async def test_404_raises_not_found(self) -> None:
        body = {"error": {"code": "not_found", "message": "not found"}}
        respx.get(f"{BASE}/v1/categories/BADID").mock(return_value=httpx.Response(404, json=body))
        async with TrendStormClient(api_key=API_KEY, base_url=BASE, max_retries=0) as ts:
            with pytest.raises(NotFound):
                await ts.categories.get("BADID")


@pytest.mark.unit
class TestSourcesEndpoints:
    @respx.mock
    async def test_add_source(self) -> None:
        respx.post(f"{BASE}/v1/sources").mock(return_value=httpx.Response(201, json=_SOURCE))
        async with TrendStormClient(api_key=API_KEY, base_url=BASE, max_retries=0) as ts:
            src = await ts.sources.add(category_id=_CAT["id"], url="https://example.com/feed.rss")
        assert src.url == "https://example.com/feed.rss"


@pytest.mark.unit
class TestJobsEndpoints:
    @respx.mock
    async def test_create_job(self) -> None:
        respx.post(f"{BASE}/v1/jobs").mock(return_value=httpx.Response(202, json=_JOB_ACCEPTED))
        async with TrendStormClient(api_key=API_KEY, base_url=BASE, max_retries=0) as ts:
            accepted = await ts.jobs.create(category_id=_CAT["id"])
        assert accepted.job_id == _JOB_ACCEPTED["job_id"]

    @respx.mock
    async def test_get_job(self) -> None:
        respx.get(f"{BASE}/v1/jobs/{_JOB['id']}").mock(return_value=httpx.Response(200, json=_JOB))
        async with TrendStormClient(api_key=API_KEY, base_url=BASE, max_retries=0) as ts:
            job = await ts.jobs.get(_JOB["id"])
        assert job.status.value == "completed"
        assert job.metrics.documents_ingested == 3

    @respx.mock
    async def test_list_jobs(self) -> None:
        payload = {"jobs": [_JOB], "next_cursor": None}
        respx.get(f"{BASE}/v1/jobs").mock(return_value=httpx.Response(200, json=payload))
        async with TrendStormClient(api_key=API_KEY, base_url=BASE, max_retries=0) as ts:
            resp = await ts.jobs.list()
        assert len(resp.jobs) == 1


@pytest.mark.unit
class TestQuotaEndpoints:
    @respx.mock
    async def test_current_month(self) -> None:
        payload = {
            "allowed": True,
            "monthly_spend_usd": 1.23,
            "monthly_limit_usd": 50.0,
            "jobs_this_month": 5,
            "jobs_limit": 100,
            "reason": None,
        }
        respx.get(f"{BASE}/v1/quota").mock(return_value=httpx.Response(200, json=payload))
        async with TrendStormClient(api_key=API_KEY, base_url=BASE, max_retries=0) as ts:
            quota = await ts.quota.current_month()
        assert quota.allowed is True
        assert quota.monthly_spend_usd == pytest.approx(1.23)


@pytest.mark.unit
class TestAuthHeader:
    @respx.mock
    async def test_bearer_header_sent(self) -> None:
        route = respx.get(f"{BASE}/v1/quota").mock(return_value=httpx.Response(200, json={
            "allowed": True, "monthly_spend_usd": 0, "monthly_limit_usd": 0,
            "jobs_this_month": 0, "jobs_limit": 0,
        }))
        async with TrendStormClient(api_key=API_KEY, base_url=BASE, max_retries=0) as ts:
            await ts.quota.current_month()
        assert route.called
        sent = route.calls[0].request
        assert sent.headers.get("authorization") == f"Bearer {API_KEY}"


@pytest.mark.unit
class TestClientNotOpen:
    async def test_request_without_context_manager_raises(self) -> None:
        ts = TrendStormClient(api_key=API_KEY, base_url=BASE)
        with pytest.raises(RuntimeError, match="not open"):
            await ts._request("GET", "/v1/quota")
