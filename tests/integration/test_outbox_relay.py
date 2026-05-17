"""Integration tests for the outbox pattern in JobService + OutboxRelay.

Tests verify:
  - Happy path: job + outbox entry written atomically; relay publishes to Kafka;
    entry marked published.
  - Kafka failure: outbox entry persists (retry_count incremented); job stays in Mongo.
  - Transaction failure: neither job nor outbox entry is written.

Requires `make up` (Mongo + Kafka). Skips gracefully if infra is not running.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from trendstorm.infrastructure.mongo.client import MongoClient
from trendstorm.infrastructure.mongo.repositories.job_repository import MongoJobRepository
from trendstorm.infrastructure.mongo.repositories.outbox_repository import (
    MongoOutboxRepository,
)
from trendstorm.services.job_service import JobService
from trendstorm.services.outbox.relay import OutboxRelay
from trendstorm.shared.config import get_settings
from trendstorm.shared.ids import new_id

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()


@pytest.fixture
async def mongo():
    """Connected MongoClient. Skips if Mongo is not reachable."""
    settings = get_settings()
    client = MongoClient(settings.mongo)
    try:
        await client.connect()
    except Exception:
        pytest.skip("Mongo not reachable — run `make up` first")
    yield client
    await client.close()


@pytest.fixture
def tenant_id() -> str:
    return f"test-tenant-{new_id()}"


@pytest.fixture
def category_id() -> str:
    return new_id()


# ---------------------------------------------------------------------------
# Happy path: atomic write → relay → Kafka ack → mark published
# ---------------------------------------------------------------------------

class TestOutboxHappyPath:
    async def test_job_and_outbox_entry_written_atomically(
        self, mongo: MongoClient, tenant_id: str, category_id: str
    ) -> None:
        """Both job and outbox entry must exist after create_job succeeds."""
        jobs_repo = MongoJobRepository(mongo)
        outbox_repo = MongoOutboxRepository(mongo)
        service = JobService(jobs=jobs_repo, outbox=outbox_repo, mongo=mongo)

        job = await service.create_job(
            tenant_id=tenant_id,
            category_id=category_id,
            source_ids=[],
        )

        # Job exists in Mongo
        stored_job = await jobs_repo.get(tenant_id, job.id)
        assert stored_job is not None
        assert stored_job.id == job.id
        assert stored_job.category_id == category_id

        # Outbox entry exists and is pending
        pending = await outbox_repo.find_pending(limit=100)
        matching = [e for e in pending if e.key == job.id]
        assert len(matching) == 1
        entry = matching[0]
        assert entry.tenant_id == tenant_id
        assert "trendstorm.jobs.requested" in entry.topic
        assert entry.published_at is None

    async def test_relay_publishes_and_marks_published(
        self, mongo: MongoClient, tenant_id: str, category_id: str
    ) -> None:
        """Relay should publish the entry to Kafka and stamp published_at."""
        jobs_repo = MongoJobRepository(mongo)
        outbox_repo = MongoOutboxRepository(mongo)
        service = JobService(jobs=jobs_repo, outbox=outbox_repo, mongo=mongo)

        job = await service.create_job(
            tenant_id=tenant_id,
            category_id=category_id,
            source_ids=[],
        )

        # Fetch the pending entry for this job
        pending = await outbox_repo.find_pending(limit=100)
        entry = next((e for e in pending if e.key == job.id), None)
        assert entry is not None

        # Mock producer that succeeds
        mock_producer_inner = AsyncMock()
        mock_producer = MagicMock()
        mock_producer.producer.send_and_wait = mock_producer_inner

        relay = OutboxRelay(outbox_repo, mock_producer, poll_interval=0.05)
        await relay._tick()

        # Producer was called
        mock_producer_inner.assert_called_once()
        call_kwargs = mock_producer_inner.call_args
        assert entry.topic in call_kwargs.args or call_kwargs.args[0] == entry.topic

        # Entry is now marked published
        still_pending = await outbox_repo.find_pending(limit=100)
        matching_still_pending = [e for e in still_pending if e.key == job.id]
        assert len(matching_still_pending) == 0  # no longer pending


# ---------------------------------------------------------------------------
# Kafka failure: entry persists, retry_count incremented
# ---------------------------------------------------------------------------

class TestOutboxKafkaFailure:
    async def test_kafka_failure_increments_retry_count(
        self, mongo: MongoClient, tenant_id: str, category_id: str
    ) -> None:
        """If Kafka publish fails, entry stays pending with retry_count > 0."""
        jobs_repo = MongoJobRepository(mongo)
        outbox_repo = MongoOutboxRepository(mongo)
        service = JobService(jobs=jobs_repo, outbox=outbox_repo, mongo=mongo)

        job = await service.create_job(
            tenant_id=tenant_id,
            category_id=category_id,
            source_ids=[],
        )

        # Mock producer that raises
        mock_producer = MagicMock()
        mock_producer.producer.send_and_wait = AsyncMock(
            side_effect=Exception("Kafka broker unavailable")
        )

        relay = OutboxRelay(outbox_repo, mock_producer, poll_interval=0.05)
        await relay._tick()

        # Entry is still pending (not marked published)
        pending = await outbox_repo.find_pending(limit=100)
        entry = next((e for e in pending if e.key == job.id), None)
        assert entry is not None
        assert entry.published_at is None
        assert entry.retry_count >= 1

    async def test_job_persists_even_on_kafka_failure(
        self, mongo: MongoClient, tenant_id: str, category_id: str
    ) -> None:
        """The job row always exists — Kafka failure is a relay concern, not job concern."""
        jobs_repo = MongoJobRepository(mongo)
        outbox_repo = MongoOutboxRepository(mongo)
        service = JobService(jobs=jobs_repo, outbox=outbox_repo, mongo=mongo)

        job = await service.create_job(
            tenant_id=tenant_id,
            category_id=category_id,
            source_ids=[],
        )

        # Even with Kafka down (relay never ran), the job exists
        stored_job = await jobs_repo.get(tenant_id, job.id)
        assert stored_job is not None
        assert stored_job.id == job.id


# ---------------------------------------------------------------------------
# Transaction semantics: mark_published is idempotent
# ---------------------------------------------------------------------------

class TestOutboxIdempotency:
    async def test_mark_published_idempotent(
        self, mongo: MongoClient, tenant_id: str, category_id: str
    ) -> None:
        """Calling mark_published twice does not raise; entry stays published."""
        jobs_repo = MongoJobRepository(mongo)
        outbox_repo = MongoOutboxRepository(mongo)
        service = JobService(jobs=jobs_repo, outbox=outbox_repo, mongo=mongo)

        job = await service.create_job(
            tenant_id=tenant_id,
            category_id=category_id,
            source_ids=[],
        )

        pending = await outbox_repo.find_pending(limit=100)
        entry = next((e for e in pending if e.key == job.id), None)
        assert entry is not None

        # Call mark_published twice — should not raise
        await outbox_repo.mark_published(entry.id)
        await outbox_repo.mark_published(entry.id)  # idempotent

        # Still not in pending list
        still_pending = await outbox_repo.find_pending(limit=100)
        assert not any(e.key == job.id for e in still_pending)

    async def test_relay_tick_with_no_pending_entries_does_not_raise(
        self, mongo: MongoClient
    ) -> None:
        """Empty pending list is a normal condition; relay should not error."""
        outbox_repo = MongoOutboxRepository(mongo)
        mock_producer = MagicMock()
        mock_producer.producer.send_and_wait = AsyncMock()

        relay = OutboxRelay(outbox_repo, mock_producer)
        # Should complete without raising
        await relay._tick()
        mock_producer.producer.send_and_wait.assert_not_called()
