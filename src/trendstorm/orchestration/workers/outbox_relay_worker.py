"""Outbox relay worker — polls pending outbox entries and publishes to Kafka.

Unlike the standard Kafka workers (which extend BaseConsumer), this worker
does NOT subscribe to any Kafka topic. It runs a simple async polling loop
via OutboxRelay.relay_loop(), publishing entries written by JobService during
job creation.

This design is intentional:
  - The outbox pattern requires no Kafka input topic — entries arrive via Mongo.
  - BaseConsumer's offset management and idempotency machinery are irrelevant.
  - The relay's idempotency is provided by `mark_published` (once published,
    the entry's published_at is set and find_pending skips it).

Lifecycle mirrors other workers: connect clients → run loop → disconnect.
SIGTERM is caught; the relay loop is cancelled cleanly.

Run:
    python -m trendstorm.orchestration.workers.outbox_relay_worker
"""

from __future__ import annotations

import asyncio
import signal

from trendstorm.infrastructure.kafka.producer import KafkaProducerClient
from trendstorm.infrastructure.metrics.prometheus_server import MetricsServer
from trendstorm.infrastructure.mongo.client import MongoClient
from trendstorm.infrastructure.mongo.repositories.outbox_repository import (
    MongoOutboxRepository,
)
from trendstorm.services.outbox.relay import OutboxRelay
from trendstorm.shared.config import get_settings
from trendstorm.shared.logging import configure_logging, get_logger
from trendstorm.shared.tracing import configure_tracing, shutdown_tracing

logger = get_logger(__name__)


class OutboxRelayWorker:
    """Worker process that runs the OutboxRelay polling loop."""

    def __init__(
        self,
        *,
        relay: OutboxRelay,
        metrics_port: int = 9090,
    ) -> None:
        self._relay = relay
        self._metrics_port = metrics_port
        self._stop_event = asyncio.Event()
        self._metrics_server: MetricsServer | None = None

    async def start(self) -> None:
        self._metrics_server = MetricsServer(port=self._metrics_port)
        await self._metrics_server.start()
        logger.info("outbox_relay_worker_started", metrics_port=self._metrics_port)

    async def stop(self) -> None:
        self._relay.stop()
        self._stop_event.set()
        if self._metrics_server is not None:
            await self._metrics_server.stop()
        logger.info("outbox_relay_worker_stopped")

    async def run(self) -> None:
        """Run the relay loop until stopped."""
        await self.start()
        try:
            await self._relay.relay_loop(stop_event=self._stop_event)
        finally:
            await self.stop()


# ===========================================================================
# Process entry point
# ===========================================================================


async def run_worker() -> None:
    settings = get_settings()
    configure_logging()
    configure_tracing(service_name="trendstorm-outbox-relay")
    logger.info("outbox_relay_worker_booting")

    mongo = MongoClient(settings.mongo)
    producer = KafkaProducerClient(settings.kafka)

    await asyncio.gather(mongo.connect(), producer.start())
    logger.info("outbox_relay_worker_clients_connected")

    repo = MongoOutboxRepository(mongo)
    relay = OutboxRelay(repo, producer)
    worker = OutboxRelayWorker(relay=relay, metrics_port=settings.kafka.metrics_port)

    stop_event = asyncio.Event()
    loop = asyncio.get_event_loop()

    def _signal_handler() -> None:
        logger.info("outbox_relay_worker_shutdown_signal")
        stop_event.set()

    loop.add_signal_handler(signal.SIGTERM, _signal_handler)
    loop.add_signal_handler(signal.SIGINT, _signal_handler)

    try:
        await worker.run()
    finally:
        await asyncio.gather(
            mongo.close(),
            producer.stop(),
            return_exceptions=True,
        )
        shutdown_tracing()
        logger.info("outbox_relay_worker_shutdown_complete")


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
