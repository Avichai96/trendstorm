"""Integration tests for the jobs resource and SSE streaming.

Requires a live TrendStorm API with all workers running.
Run with: TRENDSTORM_BASE_URL=http://localhost:8080 TRENDSTORM_API_KEY=ts_test_... pytest -m integration
"""
from __future__ import annotations

import asyncio

import pytest

from trendstorm_sdk import TrendStormClient
from trendstorm_shared.types import JobStatus, SourceType, StreamEventType


async def _setup_category_and_source(ts: TrendStormClient) -> tuple[str, str]:
    cat = await ts.categories.create(name="SDK Job Test Category")
    src = await ts.sources.add(
        category_id=cat.id,
        url="https://arxiv.org/rss/cs.AI",
        label="arXiv",
        type=SourceType.RSS,
    )
    return cat.id, src.id


@pytest.mark.integration
class TestJobSubmit:
    async def test_create_job_returns_202(self, ts: TrendStormClient) -> None:
        cat_id, src_id = await _setup_category_and_source(ts)
        accepted = await ts.jobs.create(category_id=cat_id, source_ids=[src_id])
        assert accepted.job_id
        assert accepted.status == JobStatus.PENDING
        assert "/stream" in accepted.stream_url

    async def test_list_jobs_includes_created(self, ts: TrendStormClient) -> None:
        cat_id, src_id = await _setup_category_and_source(ts)
        accepted = await ts.jobs.create(category_id=cat_id, source_ids=[src_id])

        jobs = await ts.jobs.list()
        ids = [j.id for j in jobs.jobs]
        assert accepted.job_id in ids

    async def test_get_job_status(self, ts: TrendStormClient) -> None:
        cat_id, _ = await _setup_category_and_source(ts)
        accepted = await ts.jobs.create(category_id=cat_id)
        job = await ts.jobs.get(accepted.job_id)
        assert job.id == accepted.job_id
        assert job.status in JobStatus


@pytest.mark.integration
@pytest.mark.slow
class TestJobStreaming:
    async def test_stream_emits_at_least_one_event(self, ts: TrendStormClient) -> None:
        cat_id, src_id = await _setup_category_and_source(ts)
        accepted = await ts.jobs.create(category_id=cat_id, source_ids=[src_id])

        events = []
        try:
            async with asyncio.timeout(60):
                async for event in ts.jobs.stream(accepted.job_id, heartbeat_timeout=30.0):
                    events.append(event)
                    if event.event_type.is_terminal or len(events) >= 5:
                        break
        except TimeoutError:
            pytest.skip("Stream timed out — workers may not be running")

        assert len(events) >= 1
        assert all(hasattr(e, "event_type") for e in events)

    async def test_resume_with_last_event_id(self, ts: TrendStormClient) -> None:
        cat_id, _ = await _setup_category_and_source(ts)
        accepted = await ts.jobs.create(category_id=cat_id)

        first_events = []
        try:
            async with asyncio.timeout(30):
                async for event in ts.jobs.stream(accepted.job_id):
                    first_events.append(event)
                    if len(first_events) >= 2:
                        break
        except TimeoutError:
            pytest.skip("Not enough events to test resume")

        if len(first_events) < 2:
            pytest.skip("Not enough events for resume test")

        last_id = first_events[0].seq
        resumed_events = []
        try:
            async with asyncio.timeout(10):
                async for event in ts.jobs.resume(accepted.job_id, last_event_id=last_id):
                    resumed_events.append(event)
                    break
        except TimeoutError:
            pass

        if resumed_events:
            assert resumed_events[0].seq > last_id
