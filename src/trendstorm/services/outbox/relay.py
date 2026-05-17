"""OutboxRelay — polls pending outbox entries and publishes them to Kafka.

The relay runs in a tight async loop (poll every 500ms). On each tick it:
  1. Fetches up to `batch_size` unpublished entries.
  2. For each entry, publishes to the designated Kafka topic + key.
  3. On success: stamps `published_at=now()`.
  4. On Kafka failure: increments `retry_count`, logs, and moves on.

Kafka failures are expected to be transient (broker restart, rebalance).
The entry stays pending and will be retried on the next tick. Entries that
accumulate high `retry_count` are surfaced by the runbook monitoring query:
  db.outbox.find({published_at: null, retry_count: {$gt: 5}}).count()

The relay does NOT have a DLQ. If an entry can't be published after many
retries, it sits in the outbox until an operator investigates. Data is not
silently discarded.
"""
from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from opentelemetry import trace

from trendstorm.domain.outbox.models import OutboxEntry
from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    from trendstorm.domain.outbox.repository import OutboxRepository
    from trendstorm.infrastructure.kafka.producer import KafkaProducerClient

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)

_DEFAULT_POLL_INTERVAL = 0.5   # seconds
_DEFAULT_BATCH_SIZE = 100
_MAX_RETRY_LOG_THRESHOLD = 5   # log a warning when entry exceeds this


class OutboxRelay:
    """Polls the outbox collection and publishes pending entries to Kafka.

    Args:
        repo:           OutboxRepository to poll.
        producer:       KafkaProducerClient (already started).
        poll_interval:  Seconds between poll ticks (default 0.5).
        batch_size:     Max entries per tick (default 100).

    """

    def __init__(
        self,
        repo: OutboxRepository,
        producer: KafkaProducerClient,
        *,
        poll_interval: float = _DEFAULT_POLL_INTERVAL,
        batch_size: int = _DEFAULT_BATCH_SIZE,
    ) -> None:
        self._repo = repo
        self._producer = producer
        self._poll_interval = poll_interval
        self._batch_size = batch_size
        self._running = False

    async def relay_loop(self, *, stop_event: asyncio.Event | None = None) -> None:
        """Run the relay indefinitely until `stop_event` is set (or cancelled).

        This is the main entrypoint called by the worker. If `stop_event` is
        None the loop runs until an `asyncio.CancelledError` is raised (e.g.
        SIGTERM → `worker.stop()`).
        """
        self._running = True
        logger.info("outbox_relay_started", poll_interval=self._poll_interval)
        try:
            while self._running:
                if stop_event is not None and stop_event.is_set():
                    break
                await self._tick()
                await asyncio.sleep(self._poll_interval)
        except asyncio.CancelledError:
            logger.info("outbox_relay_cancelled")
            raise
        finally:
            self._running = False
            logger.info("outbox_relay_stopped")

    def stop(self) -> None:
        """Signal the relay loop to exit after the current tick."""
        self._running = False

    async def _tick(self) -> None:
        """One relay pass: fetch pending entries and publish each."""
        with tracer.start_as_current_span("outbox.relay_tick") as span:
            try:
                entries = await self._repo.find_pending(limit=self._batch_size)
            except Exception as exc:
                logger.warning("outbox.find_pending_failed", error=str(exc))
                return

            if not entries:
                return

            span.set_attribute("outbox.batch_size", len(entries))
            published = 0
            failed = 0

            for entry in entries:
                success = await self._publish_one(entry)
                if success:
                    published += 1
                else:
                    failed += 1

            logger.info(
                "outbox.tick_complete",
                published=published,
                failed=failed,
                batch=len(entries),
            )

    async def _publish_one(self, entry: OutboxEntry) -> bool:
        """Publish one entry; return True on success."""
        try:
            payload_bytes = json.dumps(entry.payload).encode()
            key_bytes = entry.key.encode()

            await self._producer.producer.send_and_wait(
                entry.topic,
                value=payload_bytes,
                key=key_bytes,
            )

            await self._repo.mark_published(entry.id)
            logger.debug(
                "outbox.published",
                entry_id=entry.id,
                topic=entry.topic,
                key=entry.key,
            )
            return True

        except Exception as exc:
            new_count = await self._repo.increment_retry(entry.id)
            log_fn = logger.warning if new_count > _MAX_RETRY_LOG_THRESHOLD else logger.debug
            log_fn(
                "outbox.publish_failed",
                entry_id=entry.id,
                topic=entry.topic,
                retry_count=new_count,
                error=str(exc),
            )
            return False
