"""SSE Coordinator worker.

Run:
    python -m trendstorm.orchestration.workers.sse_coordinator_worker


Consumes stream.partial.v1 from Kafka, assigns job-scoped seq numbers,
writes events to Redis Streams (durable log) + Redis Pub/Sub (live fanout).

Architecture:
    All workers that want to emit stream events publish a StreamPartialEvent
    to Kafka. The SSE Coordinator is the single writer to Redis, which keeps
    the seq INCR atomic and Redis writes in one place.

Idempotency:
    Key = f"sse:{event.event_id}"
    Duplicate Kafka deliveries (at-least-once guarantee) must not increment
    seq twice. The idempotency layer ensures exactly-once seq assignment.

Retry topology:
    SSE events are ephemeral UX signals — if they fail (Redis down, parse
    error), they are sent to the DLQ rather than the tiered retry topics.
    Stream events arriving stale (e.g. 30s after job completion) are useless;
    retry delay would only make things worse.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from opentelemetry import trace

from trendstorm.domain.streaming.events import StreamEventType
from trendstorm.infrastructure.kafka.consumer import BaseConsumer
from trendstorm.infrastructure.kafka.producer import KafkaProducerClient
from trendstorm.infrastructure.mongo.client import MongoClient
from trendstorm.infrastructure.mongo.repositories import IdempotencyRepository
from trendstorm.infrastructure.redis.client import RedisClient
from trendstorm.infrastructure.redis.pubsub import RedisPubSub
from trendstorm.infrastructure.redis.streams import RedisStreamStore
from trendstorm.orchestration.events import EventEnvelope, StreamPartialEvent
from trendstorm.orchestration.topics import ConsumerGroup, Topic
from trendstorm.shared.config import KafkaSettings, SSESettings, get_settings
from trendstorm.shared.logging import configure_logging, get_logger
from trendstorm.shared.metrics.registry import METRICS
from trendstorm.shared.tracing import configure_tracing, shutdown_tracing

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)


class SSECoordinatorWorker(BaseConsumer):
    """Consumes stream.partial.v1 and fans out to Redis Streams + Pub/Sub."""

    def __init__(
        self,
        *,
        settings: KafkaSettings,
        sse_settings: SSESettings,
        stream_store: RedisStreamStore,
        pubsub: RedisPubSub,
        idempotency: IdempotencyRepository,
        producer: KafkaProducerClient,
    ) -> None:
        super().__init__(
            topics=[Topic.STREAM_PARTIAL],
            group_id=ConsumerGroup.SSE_COORDINATOR.value,
            settings=settings,
            idempotency=idempotency,
            producer=producer,
            worker_name="sse-coordinator",
        )
        self._stream_store = stream_store
        self._pubsub = pubsub

    def _idempotency_key(self, event: EventEnvelope) -> str | None:
        """Use the StreamEvent's event_id so duplicate deliveries don't corrupt seq."""
        return f"sse:{event.event_id}"

    async def handle(self, event: EventEnvelope) -> None:
        if not isinstance(event, StreamPartialEvent):
            logger.warning("sse_coordinator_unexpected_event", event_type=getattr(event, "event_type", "unknown"))
            return

        with tracer.start_as_current_span(
            "sse.coordinator.handle",
            attributes={"job_id": event.job_id, "stream_event_type": event.stream_event_type},
        ):
            await self._process(event)

    def _record_handle_metrics(
        self, event: EventEnvelope, status: str, elapsed: float
    ) -> None:
        event_type = getattr(event, "stream_event_type", getattr(event, "event_type", "unknown"))
        METRICS.sse_events.labels(
            tenant_id=event.tenant_id,
            event_type=event_type,
            status=status,
        ).inc()
        METRICS.sse_event_duration.labels(
            tenant_id=event.tenant_id,
            status=status,
        ).observe(elapsed)

    async def _process(self, event: StreamPartialEvent) -> None:
        # Assign monotonic seq for this job.
        seq = await self._stream_store.incr_seq(event.job_id)

        # Build the payload dict that goes into Redis Streams and Pub/Sub.
        payload: dict[str, Any] = {
            "event_id": event.event_id,
            "job_id": event.job_id,
            "tenant_id": event.tenant_id,
            "event_type": event.stream_event_type,
            "seq": seq,
            "stage": event.stage,
            "payload": event.stream_payload,
            "occurred_at": event.occurred_at.isoformat(),
        }

        # Write to Redis Streams (durable log — replay on reconnect).
        await self._stream_store.append(event.job_id, payload)

        # Publish to Pub/Sub (live fanout — connected SSE clients wake up).
        await self._pubsub.publish(event.job_id, payload)

        logger.debug(
            "sse_event_written",
            job_id=event.job_id,
            seq=seq,
            stream_event_type=event.stream_event_type,
        )

        # Validate event_type is known (best-effort — unknown types still store).
        try:
            StreamEventType(event.stream_event_type)
        except ValueError:
            logger.warning(
                "sse_unknown_event_type",
                stream_event_type=event.stream_event_type,
            )


# ===========================================================================
# Process entry point
# ===========================================================================

async def run_worker() -> None:
    """Start the SSE coordinator worker, blocking until shutdown signal."""
    settings = get_settings()
    configure_logging()
    configure_tracing(service_name="trendstorm-sse-coordinator")
    logger.info("sse_coordinator_worker_booting")

    redis = RedisClient(settings.redis)
    mongo = MongoClient(settings.mongo)
    producer = KafkaProducerClient(settings.kafka)

    await asyncio.gather(
        redis.connect(),
        mongo.connect(),
        producer.start(),
    )

    stream_store = RedisStreamStore(settings.sse)
    stream_store.init(redis.client)

    pubsub = RedisPubSub(settings.sse)
    pubsub.init(redis.client)

    idem = IdempotencyRepository(mongo)

    worker = SSECoordinatorWorker(
        settings=settings.kafka,
        sse_settings=settings.sse,
        stream_store=stream_store,
        pubsub=pubsub,
        idempotency=idem,
        producer=producer,
    )
    await worker.start()
    worker.install_signal_handlers()

    try:
        await worker.run()
    finally:
        logger.info("sse_coordinator_worker_shutting_down")
        await worker.stop()
        await producer.stop()
        await redis.close()
        await mongo.close()
        shutdown_tracing()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_worker())


if __name__ == "__main__":
    main()
