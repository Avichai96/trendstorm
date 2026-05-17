"""Full Phase 9 pipeline integration test: Publisher + SSE Coordinator.

Exercises the complete streaming pipeline:
    1. Insert a pre-built Analysis + Category into Mongo.
    2. Spawn PublisherWorker + SSECoordinatorWorker with real infra.
    3. Publish PublishPendingEvent to Kafka.
    4. PublisherWorker: renders MD+JSON, uploads to MinIO, publishes
       StreamPartialEvent(STAGE_STARTED) and StreamPartialEvent(REPORT_READY)
       to stream.partial.v1.
    5. SSECoordinatorWorker: consumes stream.partial.v1, writes to Redis Streams
       + Pub/Sub.
    6. Consume PublishCompletedEvent from publish.completed.v1.
    7. Verify:
       - Reports exist in Mongo (md + json ids from completed event).
       - Redis Streams has STAGE_STARTED + REPORT_READY for the job.
       - PublishCompletedEvent.success=True.

Skip semantics:
    - Infrastructure unavailable (Mongo/Kafka/Redis/MinIO) → skip.
    - No infrastructure: test skipped gracefully.

This test is SLOW (30-60s). Run manually:
    uv run pytest tests/integration/test_full_pipeline_with_sse.py \
        -m "integration and slow" -s
"""
from __future__ import annotations

import asyncio
import contextlib

import pytest
from aiokafka import AIOKafkaConsumer

from trendstorm.agents.publisher.pipeline import PublisherPipeline
from trendstorm.domain.analyses.models import Analysis, Citation, Insight
from trendstorm.domain.categories.models import Category
from trendstorm.domain.streaming.events import StreamEventType
from trendstorm.infrastructure.blob.minio_client import MinioClient
from trendstorm.infrastructure.kafka.producer import KafkaProducerClient
from trendstorm.infrastructure.mongo.client import MongoClient
from trendstorm.infrastructure.mongo.repositories import (
    IdempotencyRepository,
    MongoAnalysisRepository,
    MongoCategoryRepository,
    MongoReportRepository,
)
from trendstorm.infrastructure.redis.client import RedisClient
from trendstorm.infrastructure.redis.pubsub import RedisPubSub
from trendstorm.infrastructure.redis.streams import RedisStreamStore
from trendstorm.orchestration.events import PublishCompletedEvent, PublishPendingEvent
from trendstorm.orchestration.topics import Topic
from trendstorm.orchestration.workers.publisher_worker import PublisherWorker
from trendstorm.orchestration.workers.sse_coordinator_worker import SSECoordinatorWorker
from trendstorm.shared.config import get_settings
from trendstorm.shared.ids import new_id

pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.asyncio]

_TENANT_ID = "phase9-e2e-tenant"
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
            "AI safety is advancing with RLHF and Constitutional AI as dominant "
            "alignment techniques. Mechanistic interpretability research using sparse "
            "autoencoders is maturing. Capability evaluations are shifting toward "
            "agentic, multi-step task suites."
        ),
        insights=[
            Insight(
                claim="RLHF and Constitutional AI are the dominant alignment techniques at frontier labs in 2025.",
                rationale="OpenAI, Anthropic, and Google all deploy variants of these techniques.",
                supporting_chunk_ids=[_CHUNK_ID],
                confidence=0.92,
                tags=["rlhf", "alignment", "constitutional-ai"],
            ),
            Insight(
                claim="Sparse autoencoders enable mechanistic interpretability of LLM activations.",
                rationale="Recent research decomposes activations into interpretable features.",
                supporting_chunk_ids=[_CHUNK_ID],
                confidence=0.85,
                tags=["interpretability", "sparse-autoencoders"],
            ),
        ],
        citations=[
            Citation(
                chunk_id=_CHUNK_ID,
                document_id=_DOC_ID,
                source_id=_SOURCE_ID,
                excerpt="RLHF is the dominant LLM alignment technique used by major AI labs.",
                url="https://example.com/alignment",
            )
        ],
        validator_score=0.88,
        validator_passed=True,
        validator_notes="Well-grounded claims with strong source support.",
        model_name="claude-haiku-4-5-20251001",
        model_provider="anthropic",
    )


def _make_category() -> Category:
    return Category(
        id=_CATEGORY_ID,
        tenant_id=_TENANT_ID,
        name="AI Safety Phase9 E2E",
        description="Full pipeline integration test for Phase 9.",
        keywords=["alignment", "rlhf", "safety", "interpretability"],
    )


@pytest.fixture
async def stack():
    """Full infrastructure stack: Mongo + MinIO + Redis + Kafka + two workers."""
    settings = get_settings()

    mongo = MongoClient(settings.mongo)
    minio = MinioClient(settings.blob)
    redis = RedisClient(settings.redis)
    producer = KafkaProducerClient(settings.kafka)

    try:
        await asyncio.gather(
            mongo.connect(),
            minio.connect(),
            redis.connect(),
            producer.start(),
        )
    except Exception as exc:
        pytest.skip(f"Infrastructure not reachable: {exc}")

    analysis_repo = MongoAnalysisRepository(mongo)
    category_repo = MongoCategoryRepository(mongo)
    report_repo = MongoReportRepository(mongo)
    idem = IdempotencyRepository(mongo)

    # Insert test data.
    try:
        await analysis_repo.insert(_make_analysis())
        await category_repo.insert(_make_category())
    except Exception:
        pass  # Already exists from a previous slow run.

    # Build publisher pipeline + worker.
    pipeline = PublisherPipeline(
        analysis_repo=analysis_repo,
        category_repo=category_repo,
        minio=minio,
        report_repo=report_repo,
        blob_settings=settings.blob,
    )
    publisher = PublisherWorker(
        kafka_settings=settings.kafka,
        pipeline=pipeline,
        idempotency=idem,
        producer=producer,
    )

    # Build SSE coordinator worker.
    stream_store = RedisStreamStore(settings.sse)
    stream_store.init(redis.client)
    pubsub = RedisPubSub(settings.sse)
    pubsub.init(redis.client)
    coordinator = SSECoordinatorWorker(
        settings=settings.kafka,
        sse_settings=settings.sse,
        stream_store=stream_store,
        pubsub=pubsub,
        idempotency=IdempotencyRepository(mongo),
        producer=producer,
    )

    try:
        await asyncio.gather(publisher.start(), coordinator.start())
    except Exception as exc:
        await asyncio.gather(producer.stop(), minio.close(), redis.close(), mongo.close(), return_exceptions=True)
        pytest.skip(f"Workers failed to start: {exc}")

    publisher_task = asyncio.create_task(publisher.run())
    coordinator_task = asyncio.create_task(coordinator.run())

    yield {
        "settings": settings,
        "mongo": mongo,
        "redis": redis,
        "stream_store": stream_store,
        "producer": producer,
        "report_repo": report_repo,
        "publisher": publisher,
        "coordinator": coordinator,
    }

    # Teardown.
    for task in [publisher_task, coordinator_task]:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    with contextlib.suppress(Exception):
        await publisher.stop()
    with contextlib.suppress(Exception):
        await coordinator.stop()
    with contextlib.suppress(Exception):
        await producer.stop()
    with contextlib.suppress(Exception):
        await minio.close()
    with contextlib.suppress(Exception):
        await redis.close()
    with contextlib.suppress(Exception):
        await mongo.close()


async def _drain_completed(
    settings,
    *,
    job_id: str,
    timeout: float = 45.0,  # noqa: ASYNC109  # test helper signature
) -> PublishCompletedEvent | None:
    consumer = AIOKafkaConsumer(
        Topic.PUBLISH_COMPLETED.value,
        bootstrap_servers=settings.kafka.bootstrap_servers,
        group_id=f"test-phase9-{new_id()}",
        auto_offset_reset="earliest",
        enable_auto_commit=True,
    )
    await consumer.start()
    try:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            records = await asyncio.wait_for(
                consumer.getmany(timeout_ms=1000), timeout=2.0
            )
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


@pytest.mark.slow
async def test_publisher_renders_and_sse_coordinator_writes_streams(stack) -> None:
    """Full Phase 9 pipeline: PublishPendingEvent → render → SSE events in Redis Streams."""
    settings = stack["settings"]
    producer = stack["producer"]
    report_repo: MongoReportRepository = stack["report_repo"]
    stream_store: RedisStreamStore = stack["stream_store"]

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

    # Wait for PublishCompletedEvent.
    completed = await _drain_completed(settings, job_id=_JOB_ID, timeout=45.0)

    assert completed is not None, "PublishCompletedEvent not received within timeout"
    assert completed.success is True, f"Publisher failed: {completed.error_code}: {completed.error_message}"
    assert completed.markdown_report_id is not None
    assert completed.json_report_id is not None

    # Verify Mongo Report documents.
    md_report = await report_repo.get(_TENANT_ID, completed.markdown_report_id)
    assert md_report is not None
    assert md_report.tenant_id == _TENANT_ID
    assert md_report.job_id == _JOB_ID

    json_report = await report_repo.get(_TENANT_ID, completed.json_report_id)
    assert json_report is not None

    # Give the SSE coordinator time to process the stream.partial.v1 events.
    await asyncio.sleep(5)

    # Verify Redis Streams received the SSE events.
    stored = await stream_store.read_from(_JOB_ID, min_seq=0)
    event_types = [e.get("event_type") for e in stored]
    assert StreamEventType.STAGE_STARTED.value in event_types, (
        f"STAGE_STARTED missing from Redis Streams. Got: {event_types}"
    )
    assert StreamEventType.REPORT_READY.value in event_types, (
        f"REPORT_READY missing from Redis Streams. Got: {event_types}"
    )

    # Verify seq numbers are monotonically assigned.
    seqs = [e.get("seq", 0) for e in stored]
    assert seqs == sorted(seqs), f"Seq numbers not monotonic: {seqs}"
    assert len(set(seqs)) == len(seqs), f"Duplicate seq numbers: {seqs}"
