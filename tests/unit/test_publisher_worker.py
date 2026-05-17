"""Unit tests for PublisherWorker — idempotency, handler, retry routing.

All dependencies (pipeline, repos, Kafka producer) are mocked.
No Docker, no MinIO, no weasyprint.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from trendstorm.agents.publisher.pipeline import PublishPipelineResult
from trendstorm.orchestration.events import (
    AnalysisPendingEvent,
    PublishCompletedEvent,
    PublishPendingEvent,
)
from trendstorm.orchestration.topics import Topic
from trendstorm.orchestration.workers.publisher_worker import PublisherWorker
from trendstorm.services.publish.service import PublishResult
from trendstorm.shared.errors import NotFoundError
from trendstorm.shared.ids import new_id

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(
    *,
    job_id: str | None = None,
    analysis_id: str | None = None,
    category_id: str | None = None,
    attempt: int = 1,
    tenant_id: str = "t1",
) -> PublishPendingEvent:
    return PublishPendingEvent(
        correlation_id="cid",
        tenant_id=tenant_id,
        job_id=job_id or new_id(),
        analysis_id=analysis_id or new_id(),
        category_id=category_id or new_id(),
        attempt=attempt,
    )


def _make_pipeline_result(
    job_id: str = "job-1",
    analysis_id: str = "ana-1",
) -> PublishPipelineResult:
    return PublishPipelineResult(
        job_id=job_id,
        analysis_id=analysis_id,
        result=PublishResult(
            markdown_report_id=new_id(),
            pdf_report_id=new_id(),
            json_report_id=new_id(),
        ),
    )


def _build_worker(
    *,
    pipeline_result: PublishPipelineResult | None = None,
    pipeline_error: Exception | None = None,
) -> tuple[PublisherWorker, MagicMock]:
    """Return (worker, producer_mock)."""
    pipeline = MagicMock()
    if pipeline_error is not None:
        pipeline.process = AsyncMock(side_effect=pipeline_error)
    else:
        pipeline.process = AsyncMock(return_value=pipeline_result or _make_pipeline_result())

    idempotency = MagicMock()
    producer = MagicMock()
    producer.producer = MagicMock()
    producer.producer.send_and_wait = AsyncMock()

    worker = PublisherWorker.__new__(PublisherWorker)
    worker._pipeline = pipeline
    worker._idempotency = idempotency
    worker._producer = producer
    worker._worker_name = "publisher"
    return worker, producer


def _extract_published_completion(producer: Any) -> PublishCompletedEvent:
    for call in producer.producer.send_and_wait.call_args_list:
        topic = call.args[0]
        if topic == Topic.PUBLISH_COMPLETED.value:
            return PublishCompletedEvent.model_validate_json(call.kwargs["value"])
    raise AssertionError(
        f"No {Topic.PUBLISH_COMPLETED.value} call found in "
        f"{[c.args[0] for c in producer.producer.send_and_wait.call_args_list]}"
    )


# ---------------------------------------------------------------------------
# Idempotency key
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestPublisherWorkerIdempotencyKey:
    def test_key_for_publish_pending_event(self) -> None:
        worker, _ = _build_worker()
        event = _make_event(job_id="job-abc")
        assert worker._idempotency_key(event) == "publisher:job-abc"

    def test_different_jobs_have_different_keys(self) -> None:
        worker, _ = _build_worker()
        e1 = _make_event(job_id="job-1")
        e2 = _make_event(job_id="job-2")
        assert worker._idempotency_key(e1) != worker._idempotency_key(e2)

    def test_fallback_key_for_other_event_types(self) -> None:
        worker, _ = _build_worker()
        other = AnalysisPendingEvent(
            correlation_id="c", tenant_id="t", job_id="j", category_id="cat",
        )
        key = worker._idempotency_key(other)
        assert key is not None
        assert key.startswith("publisher:")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestPublisherWorkerHandle:
    async def test_calls_pipeline_with_correct_args(self) -> None:
        worker, _ = _build_worker()
        event = _make_event(
            tenant_id="t1", job_id="job-1",
            analysis_id="ana-1", category_id="cat-1",
        )
        await worker.handle(event)
        worker._pipeline.process.assert_called_once_with(  # type: ignore[attr-defined]
            tenant_id="t1",
            job_id="job-1",
            analysis_id="ana-1",
            category_id="cat-1",
        )

    async def test_publishes_completed_event_on_success(self) -> None:
        result = _make_pipeline_result(job_id="job-1", analysis_id="ana-1")
        worker, producer = _build_worker(pipeline_result=result)
        await worker.handle(_make_event(job_id="job-1", analysis_id="ana-1"))
        completed = _extract_published_completion(producer)
        assert completed.success is True

    async def test_completion_has_report_ids(self) -> None:
        result = _make_pipeline_result()
        worker, producer = _build_worker(pipeline_result=result)
        await worker.handle(_make_event())
        completed = _extract_published_completion(producer)
        assert completed.markdown_report_id == result.result.markdown_report_id
        assert completed.json_report_id == result.result.json_report_id
        assert completed.pdf_report_id == result.result.pdf_report_id

    async def test_completion_uses_job_id_as_kafka_key(self) -> None:
        worker, producer = _build_worker()
        event = _make_event(job_id="job-xyz")
        await worker.handle(event)
        for call in producer.producer.send_and_wait.call_args_list:
            assert call.kwargs["key"] == b"job-xyz"

    async def test_completion_propagates_correlation_id(self) -> None:
        worker, producer = _build_worker()
        event = PublishPendingEvent(
            correlation_id="cid-unique",
            tenant_id="t1",
            job_id="j1",
            analysis_id="a1",
            category_id="c1",
        )
        await worker.handle(event)
        completed = _extract_published_completion(producer)
        assert completed.correlation_id == "cid-unique"

    async def test_non_pending_event_ignored(self) -> None:
        worker, _ = _build_worker()
        other = AnalysisPendingEvent(
            correlation_id="c", tenant_id="t", job_id="j", category_id="cat",
        )
        await worker.handle(other)
        worker._pipeline.process.assert_not_called()  # type: ignore[attr-defined]

    async def test_stream_events_emitted(self) -> None:
        """STAGE_STARTED and REPORT_READY events are published to Kafka."""
        worker, producer = _build_worker()
        await worker.handle(_make_event(job_id="job-1"))
        topics = [c.args[0] for c in producer.producer.send_and_wait.call_args_list]
        # stream.partial events AND publish.completed expected
        stream_calls = [t for t in topics if "stream.partial" in t]
        assert len(stream_calls) >= 2  # STAGE_STARTED + REPORT_READY

    async def test_pdf_none_still_succeeds(self) -> None:
        result = PublishPipelineResult(
            job_id="job-1",
            analysis_id="ana-1",
            result=PublishResult(
                markdown_report_id=new_id(),
                pdf_report_id=None,  # PDF failed
                json_report_id=new_id(),
            ),
        )
        worker, producer = _build_worker(pipeline_result=result)
        await worker.handle(_make_event())
        completed = _extract_published_completion(producer)
        assert completed.success is True
        assert completed.pdf_report_id is None
        assert completed.markdown_report_id is not None


# ---------------------------------------------------------------------------
# Permanent failure (NotFoundError)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestPublisherWorkerPermanentFailure:
    async def test_not_found_publishes_failure(self) -> None:
        worker, producer = _build_worker(
            pipeline_error=NotFoundError("analysis not found")
        )
        await worker.handle(_make_event())
        completed = _extract_published_completion(producer)
        assert completed.success is False

    async def test_not_found_sets_error_code(self) -> None:
        worker, producer = _build_worker(
            pipeline_error=NotFoundError("analysis not found")
        )
        await worker.handle(_make_event())
        completed = _extract_published_completion(producer)
        assert completed.error_code == "not_found"

    async def test_unexpected_exception_propagates_for_retry(self) -> None:
        worker, _ = _build_worker(pipeline_error=RuntimeError("transient minio hiccup"))
        with pytest.raises(RuntimeError):
            await worker.handle(_make_event())
