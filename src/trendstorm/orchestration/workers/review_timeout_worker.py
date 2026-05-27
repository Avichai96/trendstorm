"""Review timeout sweeper worker — auto-expires pending reviews past their SLA.

The sweeper runs as a standalone polling loop (no Kafka input topic). Every
`poll_interval_seconds` it queries the `reviews` collection for pending entries
whose `timeout_at < now()`, marks each as `timed_out`, and publishes a
ReviewResolvedEvent(decision=reject) to `trendstorm.review.resolved.v1` so the
OrchestratorWorker can update the job status to REJECTED.

Scaling:
    Always deploy exactly 1 replica (`strategy: Recreate`). Two replicas do not
    cause data corruption — the `mark_timed_out` findOneAndUpdate query
    atomically checks status=pending before updating, preventing double-expiry.
    But double-publishing to Kafka and double-emitting SSE events would confuse
    reviewers. Keep it single-replica.

Prometheus alert:
    `PendingReviewsAgingHigh` fires when the oldest pending review is within
    80% of its SLA window — this covers the sweeper being down or lagging.
"""
from __future__ import annotations

import asyncio
import signal

from opentelemetry.propagate import inject

from trendstorm.domain.reviews.models import ReviewStatus
from trendstorm.infrastructure.kafka.producer import KafkaProducerClient
from trendstorm.infrastructure.metrics.prometheus_server import MetricsServer
from trendstorm.infrastructure.mongo.client import MongoClient
from trendstorm.infrastructure.mongo.repositories import MongoReviewRepository
from trendstorm.orchestration.events import ReviewResolvedEvent
from trendstorm.orchestration.topics import Topic
from trendstorm.shared.config import get_settings
from trendstorm.shared.ids import new_id
from trendstorm.shared.logging import configure_logging, get_logger
from trendstorm.shared.metrics.registry import METRICS
from trendstorm.shared.tracing import configure_tracing, shutdown_tracing

logger = get_logger(__name__)


class ReviewTimeoutSweeper:
    """Polling loop that auto-expires overdue pending reviews."""

    def __init__(
        self,
        *,
        review_repo: MongoReviewRepository,
        producer: KafkaProducerClient,
        poll_interval_seconds: int = 60,
        batch_size: int = 100,
    ) -> None:
        self._reviews = review_repo
        self._producer = producer
        self._poll_interval = poll_interval_seconds
        self._batch_size = batch_size
        self._running = False

    def stop(self) -> None:
        self._running = False

    async def sweep_loop(self, *, stop_event: asyncio.Event) -> None:
        """Run until stop_event is set. Polls every poll_interval_seconds."""
        self._running = True
        logger.info("review_timeout_sweeper.started", interval_s=self._poll_interval)

        while not stop_event.is_set():
            try:
                await self._sweep_once()
            except Exception:
                logger.exception("review_timeout_sweeper.error")

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                pass  # normal — timeout means poll again

        logger.info("review_timeout_sweeper.stopped")

    async def _sweep_once(self) -> None:
        """Find and expire all overdue pending reviews in one batch."""
        expired = await self._reviews.list_expired_pending(limit=self._batch_size)
        if not expired:
            return

        logger.info("review_timeout_sweeper.sweep", n=len(expired))

        for review in expired:
            try:
                updated = await self._reviews.mark_timed_out(review.tenant_id, review.id)
                if updated is None:
                    # Concurrently resolved — skip.
                    continue

                otel_carrier: dict[str, str] = {}
                inject(otel_carrier)
                event = ReviewResolvedEvent(
                    correlation_id=new_id(),
                    tenant_id=review.tenant_id,
                    traceparent=otel_carrier.get("traceparent"),
                    job_id=review.job_id,
                    review_id=review.id,
                    decision="reject",
                    comment="Review timed out — no decision received within the SLA window.",
                    resolved_by="timeout_sweeper",
                )
                await self._producer.producer.send_and_wait(
                    Topic.REVIEW_RESOLVED.value,
                    value=event.model_dump_json().encode(),
                    key=review.job_id.encode(),
                )

                METRICS.review_timeout_total.inc()
                logger.warning(
                    "review_timeout_sweeper.expired",
                    review_id=review.id,
                    job_id=review.job_id,
                    tenant_id=review.tenant_id,
                )
            except Exception:
                logger.exception(
                    "review_timeout_sweeper.expire_failed",
                    review_id=review.id,
                    job_id=review.job_id,
                )


class ReviewTimeoutWorker:
    """Worker process that runs the ReviewTimeoutSweeper polling loop."""

    def __init__(
        self,
        *,
        sweeper: ReviewTimeoutSweeper,
        metrics_port: int = 9090,
    ) -> None:
        self._sweeper = sweeper
        self._metrics_port = metrics_port
        self._stop_event = asyncio.Event()
        self._metrics_server: MetricsServer | None = None

    async def start(self) -> None:
        self._metrics_server = MetricsServer(port=self._metrics_port)
        await self._metrics_server.start()
        logger.info("review_timeout_worker_started", metrics_port=self._metrics_port)

    async def stop(self) -> None:
        self._sweeper.stop()
        self._stop_event.set()
        if self._metrics_server is not None:
            await self._metrics_server.stop()
        logger.info("review_timeout_worker_stopped")

    async def run(self) -> None:
        await self.start()
        try:
            await self._sweeper.sweep_loop(stop_event=self._stop_event)
        finally:
            await self.stop()


# ===========================================================================
# Process entry point
# ===========================================================================

async def run_worker() -> None:
    settings = get_settings()
    configure_logging()
    configure_tracing(service_name="trendstorm-review-timeout")

    mongo = MongoClient(settings.mongo)
    await mongo.connect()

    producer = KafkaProducerClient(settings.kafka)
    await producer.start()

    review_repo = MongoReviewRepository(mongo)
    sweeper = ReviewTimeoutSweeper(
        review_repo=review_repo,
        producer=producer,
        poll_interval_seconds=settings.hitl.sweeper_interval_seconds,
    )
    worker = ReviewTimeoutWorker(sweeper=sweeper)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _shutdown(signum: int, _: object) -> None:
        logger.info("review_timeout_worker.shutdown_signal", signal=signum)
        loop.call_soon_threadsafe(stop_event.set)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        await worker.run()
    finally:
        await producer.stop()
        await mongo.disconnect()
        shutdown_tracing()


if __name__ == "__main__":
    asyncio.run(run_worker())
