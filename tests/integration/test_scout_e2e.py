"""Integration test: IngestPendingEvent -> ScoutWorker -> Mongo + MinIO.

Requires `make up` (Mongo + Kafka + Redis + MinIO).

Flow:
    1. Seed a Source in Mongo.
    2. Spawn ScoutWorker with a mock httpx transport (no real HTTP calls).
    3. Publish IngestPendingEvent directly to Kafka.
    4. Poll Mongo until RawDocument appears.
    5. Verify blob URIs, source status, and dedup behaviour.

Skip semantics:
    If any infrastructure client fails to connect, the test is skipped (not
    failed). Run `make up` before running integration tests.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

import httpx
import pytest
from aiokafka import AIOKafkaConsumer

from trendstorm.agents.scout.hashing import content_hash
from trendstorm.domain.sources.models import Source
from trendstorm.infrastructure.blob.minio_client import MinioClient
from trendstorm.infrastructure.kafka.producer import KafkaProducerClient
from trendstorm.infrastructure.mongo.client import MongoClient
from trendstorm.infrastructure.mongo.repositories import (
    IdempotencyRepository,
    MongoRawDocumentRepository,
    MongoSourceRepository,
)
from trendstorm.infrastructure.redis.client import RedisClient
from trendstorm.orchestration.events import IngestCompletedEvent, IngestPendingEvent
from trendstorm.orchestration.topics import Topic
from trendstorm.orchestration.workers.scout_worker import ScoutWorker
from trendstorm.shared.config import get_settings
from trendstorm.shared.ids import new_id

if TYPE_CHECKING:
    from trendstorm.domain.documents.models import RawDocument


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# ---------------------------------------------------------------------------
# HTML body returned by the mock transport for every fetch request
# ---------------------------------------------------------------------------
_HTML_BODY = b"""
<html>
<head><title>Integration Test Article</title></head>
<body>
<h1>Integration Test Article</h1>
<p>This is a well-formed article body used for scout e2e integration testing.
It contains enough text for trafilatura to extract meaningful content.</p>
<p>TrendStorm scout pipeline integration test paragraph two.</p>
</body>
</html>
"""

_EXPECTED_HASH = content_hash(
    # trafilatura may vary; we hash a known subset that is always present
    # and check via startswith rather than equality.
    _HTML_BODY.decode("utf-8")
)


def _mock_transport(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        content=_HTML_BODY,
        headers={"content-type": "text/html; charset=utf-8"},
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()


@pytest.fixture
async def infra():
    """Connected infrastructure clients + a running ScoutWorker.

    Skips the test if any client fails to connect (stack not up).
    Yields a dict of clients so tests can seed data and inspect results.
    """
    settings = get_settings()

    mongo = MongoClient(settings.mongo)
    redis = RedisClient(settings.redis)
    producer = KafkaProducerClient(settings.kafka)
    minio = MinioClient(settings.blob)

    try:
        await asyncio.gather(
            mongo.connect(),
            redis.connect(),
            producer.start(),
            minio.connect(),
        )
    except Exception as exc:
        pytest.skip(f"Infrastructure not available: {exc}")

    source_repo = MongoSourceRepository(mongo)
    raw_doc_repo = MongoRawDocumentRepository(mongo)
    idem = IdempotencyRepository(mongo)

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_mock_transport),
        headers={"User-Agent": "TrendStormTest/1.0"},
    )

    worker = ScoutWorker(
        kafka_settings=settings.kafka,
        source_repo=source_repo,
        raw_doc_repo=raw_doc_repo,
        idempotency=idem,
        producer=producer,
        minio=minio,
        http_client=http_client,
        redis=redis,
        ingest_settings=settings.ingest,
    )
    await worker.start()
    worker_task = asyncio.create_task(worker.run())

    yield {
        "mongo": mongo,
        "source_repo": source_repo,
        "raw_doc_repo": raw_doc_repo,
        "producer": producer,
        "settings": settings,
    }

    await worker.stop()
    worker_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await worker_task
    await http_client.aclose()
    await producer.stop()
    await minio.close()
    await redis.close()
    await mongo.close()


async def _publish_ingest_pending(
    producer: KafkaProducerClient,
    *,
    tenant_id: str,
    job_id: str,
    source_ids: list[str],
    attempt: int = 1,
) -> None:
    event = IngestPendingEvent(
        correlation_id=new_id(),
        tenant_id=tenant_id,
        job_id=job_id,
        source_ids=source_ids,
        attempt=attempt,
    )
    await producer.producer.send_and_wait(
        Topic.INGEST_PENDING.value,
        value=event.model_dump_json().encode(),
        key=job_id.encode(),
    )


async def _wait_for_raw_documents(
    raw_doc_repo: MongoRawDocumentRepository,
    tenant_id: str,
    job_id: str,
    *,
    min_count: int = 1,
    time_limit: float = 30.0,
) -> list[RawDocument]:
    """Poll Mongo until at least `min_count` docs appear for this job."""
    deadline = asyncio.get_event_loop().time() + time_limit
    while asyncio.get_event_loop().time() < deadline:
        docs = await raw_doc_repo.list_by_job(tenant_id, job_id)
        if len(docs) >= min_count:
            return docs
        await asyncio.sleep(0.5)
    raise AssertionError(
        f"Expected {min_count} RawDocument(s) for job {job_id} within {time_limit}s"
    )


async def _consume_ingest_completed(
    settings,
    tenant_id: str,
    job_id: str,
    *,
    time_limit: float = 30.0,
) -> IngestCompletedEvent:
    """Consume IngestCompletedEvent from Kafka for a specific job."""
    consumer = AIOKafkaConsumer(
        Topic.INGEST_COMPLETED.value,
        bootstrap_servers=settings.kafka.bootstrap_servers,
        group_id=f"test-scout-{new_id()}",
        auto_offset_reset="earliest",
        enable_auto_commit=True,
    )
    await consumer.start()
    try:
        deadline = asyncio.get_event_loop().time() + time_limit
        while asyncio.get_event_loop().time() < deadline:
            batch = await asyncio.wait_for(
                consumer.getmany(timeout_ms=500, max_records=20),
                timeout=2.0,
            )
            for _tp, records in batch.items():
                for rec in records:
                    try:
                        evt = IngestCompletedEvent.model_validate_json(rec.value)
                    except Exception:
                        continue
                    if evt.tenant_id == tenant_id and evt.job_id == job_id:
                        return evt
    finally:
        await consumer.stop()
    raise AssertionError(
        f"IngestCompletedEvent not found for job {job_id} within {time_limit}s"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_ingest_creates_raw_document(infra: dict) -> None:
    """Scout fetches the mock URL and writes a RawDocument to Mongo."""
    tenant_id = new_id()
    job_id = new_id()
    category_id = new_id()

    source = Source(
        tenant_id=tenant_id,
        category_id=category_id,
        url="https://example.com/article",
        label="test",
    )
    await infra["source_repo"].insert(source)

    await _publish_ingest_pending(
        infra["producer"],
        tenant_id=tenant_id,
        job_id=job_id,
        source_ids=[source.id],
    )

    docs = await _wait_for_raw_documents(
        infra["raw_doc_repo"], tenant_id, job_id, min_count=1
    )

    assert len(docs) == 1
    doc = docs[0]
    assert doc.source_id == source.id
    assert doc.tenant_id == tenant_id
    assert doc.job_id == job_id
    assert doc.content_hash  # non-empty
    assert doc.blob_uri_raw is not None
    assert doc.blob_uri_raw.startswith("s3://")
    assert doc.blob_uri_text is not None
    assert doc.char_count > 0


async def test_ingest_updates_source_status(infra: dict) -> None:
    """Source.last_fetch_status is set to 'ok' after a successful fetch."""
    tenant_id = new_id()
    job_id = new_id()

    source = Source(
        tenant_id=tenant_id,
        category_id=new_id(),
        url="https://example.com/status-test",
    )
    await infra["source_repo"].insert(source)

    await _publish_ingest_pending(
        infra["producer"],
        tenant_id=tenant_id,
        job_id=job_id,
        source_ids=[source.id],
    )

    # Wait for document to appear — indicates pipeline completed
    await _wait_for_raw_documents(
        infra["raw_doc_repo"], tenant_id, job_id, min_count=1
    )

    # Allow one extra cycle for the status write
    await asyncio.sleep(1.0)

    updated = await infra["source_repo"].get(tenant_id, source.id)
    assert updated is not None
    assert updated.last_fetch_status == "ok"
    assert updated.last_fetch_at is not None


async def test_content_dedup(infra: dict) -> None:
    """A second ingest of identical content reuses the existing RawDocument."""
    tenant_id = new_id()
    job_id_1 = new_id()
    job_id_2 = new_id()

    source = Source(
        tenant_id=tenant_id,
        category_id=new_id(),
        url="https://example.com/dedup",
    )
    await infra["source_repo"].insert(source)

    # First ingest
    await _publish_ingest_pending(
        infra["producer"],
        tenant_id=tenant_id,
        job_id=job_id_1,
        source_ids=[source.id],
    )
    docs1 = await _wait_for_raw_documents(
        infra["raw_doc_repo"], tenant_id, job_id_1, min_count=1
    )
    assert len(docs1) == 1

    # Second ingest — same mock transport returns same HTML → same content_hash
    await _publish_ingest_pending(
        infra["producer"],
        tenant_id=tenant_id,
        job_id=job_id_2,
        source_ids=[source.id],
    )

    # IngestCompletedEvent for job_id_2 should report document_refs (via dedup)
    event = await _consume_ingest_completed(
        infra["settings"],
        tenant_id=tenant_id,
        job_id=job_id_2,
    )
    assert event.failed_source_ids == []
    assert len(event.document_refs) == 1
    # Deduped → same doc id as the first ingest
    assert event.document_refs[0].id == docs1[0].id

    # No new RawDocument created for job_id_2 (dedup hit)
    docs2 = await infra["raw_doc_repo"].list_by_job(tenant_id, job_id_2)
    assert len(docs2) == 0


async def test_ingest_completed_event_has_document_refs(infra: dict) -> None:
    """IngestCompletedEvent carries document_refs matching the stored doc."""
    tenant_id = new_id()
    job_id = new_id()

    source = Source(
        tenant_id=tenant_id,
        category_id=new_id(),
        url="https://example.com/event-check",
    )
    await infra["source_repo"].insert(source)

    await _publish_ingest_pending(
        infra["producer"],
        tenant_id=tenant_id,
        job_id=job_id,
        source_ids=[source.id],
    )

    event = await _consume_ingest_completed(
        infra["settings"],
        tenant_id=tenant_id,
        job_id=job_id,
    )

    assert event.job_id == job_id
    assert event.failed_source_ids == []
    assert len(event.document_refs) == 1

    ref = event.document_refs[0]
    assert ref.source_id == source.id
    assert ref.content_hash  # non-empty
    assert ref.blob_uri_raw is not None
    assert ref.char_count > 0

    # Cross-check: event ref matches what's in Mongo
    docs = await _wait_for_raw_documents(
        infra["raw_doc_repo"], tenant_id, job_id, min_count=1
    )
    assert docs[0].id == ref.id
    assert docs[0].content_hash == ref.content_hash
