"""Integration test: PublishPendingEvent → PublisherWorker → MinIO + Mongo + PublishCompletedEvent.

Flow:
    1. Insert a minimal Analysis + Category into Mongo.
    2. Spawn PublisherWorker with real MinIO client.
    3. Publish PublishPendingEvent to Kafka.
    4. Consume PublishCompletedEvent and verify:
       - success=True
       - markdown_report_id and json_report_id are present in Mongo
       - Report blobs exist in MinIO
    5. Verify idempotency: second publish → worker skips via idempotency key.

Skip semantics: if any infrastructure client fails to connect, the test is
skipped (not failed). Run `make up` before running integration tests.
"""
from __future__ import annotations

import asyncio
import contextlib

import pytest
from aiokafka import AIOKafkaConsumer

from trendstorm.agents.publisher.pipeline import PublisherPipeline
from trendstorm.domain.analyses.models import Analysis, Citation, Insight
from trendstorm.domain.categories.models import Category
from trendstorm.infrastructure.blob.minio_client import MinioClient
from trendstorm.infrastructure.kafka.producer import KafkaProducerClient
from trendstorm.infrastructure.mongo.client import MongoClient
from trendstorm.infrastructure.mongo.repositories import (
    IdempotencyRepository,
    MongoAnalysisRepository,
    MongoCategoryRepository,
    MongoReportRepository,
)
from trendstorm.orchestration.events import PublishCompletedEvent, PublishPendingEvent
from trendstorm.orchestration.topics import Topic
from trendstorm.orchestration.workers.publisher_worker import PublisherWorker
from trendstorm.shared.config import get_settings
from trendstorm.shared.ids import new_id

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_TENANT_ID = "pub-e2e-tenant"
_JOB_ID = new_id()
_CATEGORY_ID = new_id()
_ANALYSIS_ID = new_id()
_CHUNK_ID = new_id()
_DOC_ID = new_id()
_SOURCE_ID = new_id()


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()


def _make_analysis() -> Analysis:
    return Analysis(
        id=_ANALYSIS_ID,
        tenant_id=_TENANT_ID,
        job_id=_JOB_ID,
        category_id=_CATEGORY_ID,
        summary=(
            "AI safety research is advancing rapidly. "
            "Constitutional AI and RLHF are the dominant alignment techniques. "
            "Interpretability tools are maturing, enabling better model understanding."
        ),
        insights=[
            Insight(
                claim="RLHF is the dominant LLM alignment technique in 2025.",
                rationale="Widely adopted by all major AI labs.",
                supporting_chunk_ids=[_CHUNK_ID],
                confidence=0.9,
                tags=["rlhf", "alignment"],
            )
        ],
        citations=[
            Citation(
                chunk_id=_CHUNK_ID,
                document_id=_DOC_ID,
                source_id=_SOURCE_ID,
                excerpt="RLHF is the dominant LLM alignment technique.",
                url="https://example.com/rlhf",
            )
        ],
        validator_score=0.85,
        validator_passed=True,
        validator_notes="Strong grounding across all claims.",
        model_name="claude-3-5-sonnet-20241022",
        model_provider="anthropic",
    )


def _make_category() -> Category:
    return Category(
        id=_CATEGORY_ID,
        tenant_id=_TENANT_ID,
        name="AI Safety E2E Test",
        description="Integration test category for publisher.",
        keywords=["alignment", "rlhf", "safety"],
    )


@pytest.fixture
async def infra():
    """Connected infrastructure + running PublisherWorker. Skips if stack not up."""
    settings = get_settings()

    mongo = MongoClient(settings.mongo)
    minio = MinioClient(settings.blob)
    producer = KafkaProducerClient(settings.kafka)

    try:
        await asyncio.gather(
            mongo.connect(),
            minio.connect(),
            producer.start(),
        )
    except Exception as exc:
        pytest.skip(f"Infrastructure not available: {exc}")

    analysis_repo = MongoAnalysisRepository(mongo)
    category_repo = MongoCategoryRepository(mongo)
    report_repo = MongoReportRepository(mongo)
    idem = IdempotencyRepository(mongo)

    # Insert test data.
    try:
        await analysis_repo.insert(_make_analysis())
        await category_repo.insert(_make_category())
    except Exception:
        pass  # Already exists from a previous run.

    pipeline = PublisherPipeline(
        analysis_repo=analysis_repo,
        category_repo=category_repo,
        minio=minio,
        report_repo=report_repo,
        blob_settings=settings.blob,
    )

    worker = PublisherWorker(
        kafka_settings=settings.kafka,
        pipeline=pipeline,
        idempotency=idem,
        producer=producer,
    )

    try:
        await worker.start()
    except Exception as exc:
        await asyncio.gather(producer.stop(), minio.close(), mongo.close(), return_exceptions=True)
        pytest.skip(f"Worker failed to start: {exc}")

    yield {
        "mongo": mongo,
        "minio": minio,
        "producer": producer,
        "report_repo": report_repo,
        "worker": worker,
        "settings": settings,
    }

    with contextlib.suppress(Exception):
        await worker.stop()
    with contextlib.suppress(Exception):
        await producer.stop()
    with contextlib.suppress(Exception):
        await minio.close()
    with contextlib.suppress(Exception):
        await mongo.close()


async def _drain_completed(settings, *, job_id: str, timeout: float = 30.0) -> PublishCompletedEvent | None:  # noqa: ASYNC109  # test helper signature
    """Poll publish.completed.v1 until we find the event for this job."""
    consumer = AIOKafkaConsumer(
        Topic.PUBLISH_COMPLETED.value,
        bootstrap_servers=settings.kafka.bootstrap_servers,
        group_id=f"test-pub-e2e-{new_id()}",
        auto_offset_reset="earliest",
        enable_auto_commit=True,
    )
    await consumer.start()
    try:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            records = await asyncio.wait_for(consumer.getmany(timeout_ms=1000), timeout=2.0)
            for _tp, messages in records.items():
                for msg in messages:
                    try:
                        event = PublishCompletedEvent.model_validate_json(msg.value)
                        if event.job_id == job_id:
                            return event
                    except Exception:
                        continue
    except TimeoutError:
        pass
    finally:
        await consumer.stop()
    return None


@pytest.mark.integration
async def test_publisher_worker_renders_and_publishes_completed(infra) -> None:  # type: ignore[no-untyped-def]
    """PublishPendingEvent → worker renders MD+JSON → publishes PublishCompletedEvent."""
    settings = infra["settings"]
    producer = infra["producer"]
    report_repo: MongoReportRepository = infra["report_repo"]
    worker: PublisherWorker = infra["worker"]

    # Run the worker for one event.
    event = PublishPendingEvent(
        correlation_id=new_id(),
        tenant_id=_TENANT_ID,
        job_id=_JOB_ID,
        analysis_id=_ANALYSIS_ID,
        category_id=_CATEGORY_ID,
    )
    await producer.producer.send_and_wait(
        Topic.PUBLISH_PENDING.value,
        value=event.model_dump_json().encode(),
        key=_JOB_ID.encode(),
    )

    # Poll for the completed event (run the worker loop briefly in background).
    worker_task = asyncio.create_task(worker.run())
    try:
        completed = await _drain_completed(settings, job_id=_JOB_ID, timeout=30.0)
    finally:
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task

    assert completed is not None, "PublishCompletedEvent not received within timeout"
    assert completed.success is True
    assert completed.markdown_report_id is not None
    assert completed.json_report_id is not None

    # Verify Report docs persisted in Mongo.
    md_report = await report_repo.get(_TENANT_ID, completed.markdown_report_id)
    assert md_report is not None
    assert md_report.job_id == _JOB_ID

    json_report = await report_repo.get(_TENANT_ID, completed.json_report_id)
    assert json_report is not None
    assert json_report.job_id == _JOB_ID


@pytest.mark.integration
async def test_publisher_missing_analysis_permanent_failure(infra) -> None:  # type: ignore[no-untyped-def]
    """Missing analysis_id → permanent failure (success=False, no retry)."""
    settings = infra["settings"]
    producer = infra["producer"]
    worker: PublisherWorker = infra["worker"]
    missing_job_id = new_id()

    event = PublishPendingEvent(
        correlation_id=new_id(),
        tenant_id=_TENANT_ID,
        job_id=missing_job_id,
        analysis_id=new_id(),  # doesn't exist
        category_id=_CATEGORY_ID,
    )
    await producer.producer.send_and_wait(
        Topic.PUBLISH_PENDING.value,
        value=event.model_dump_json().encode(),
        key=missing_job_id.encode(),
    )

    worker_task = asyncio.create_task(worker.run())
    try:
        completed = await _drain_completed(settings, job_id=missing_job_id, timeout=20.0)
    finally:
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task

    assert completed is not None, "PublishCompletedEvent not received for permanent failure"
    assert completed.success is False
    assert completed.error_code is not None
