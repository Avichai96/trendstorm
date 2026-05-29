"""Helper for workers to emit stream events via Kafka.

Usage (inside any worker's handler):
    from trendstorm.services.streaming.emit import emit_stream_event
    from trendstorm.domain.streaming.events import StreamEvent, StreamEventType

    event = StreamEvent(
        job_id=job_id,
        tenant_id=tenant_id,
        event_type=StreamEventType.STAGE_STARTED,
        stage="ingesting",
        payload={"source_count": len(source_ids)},
    )
    await emit_stream_event(event, producer=producer, correlation_id=correlation_id)

Design:
    Workers publish StreamPartialEvent to Kafka topic stream.partial.v1.
    The SSE coordinator consumes, assigns seq, writes to Redis Streams,
    and publishes to Redis Pub/Sub. This keeps Redis writes in one place.

    Errors are intentionally swallowed (best-effort): stream events are
    UX extras. A failure to emit a PROGRESS event must not fail the job.
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.propagate import inject

from trendstorm.domain.streaming.events import StreamEvent
from trendstorm.infrastructure.kafka.producer import KafkaProducerClient
from trendstorm.orchestration.events import StreamPartialEvent
from trendstorm.orchestration.topics import Topic
from trendstorm.shared.logging import get_logger

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)


async def emit_stream_event(
    event: StreamEvent,
    *,
    producer: KafkaProducerClient,
    correlation_id: str,
) -> None:
    """Publish event to stream.partial.v1 (fire-and-forget, errors swallowed).

    The EventEnvelope.event_id equals StreamEvent.event_id, providing the
    SSE coordinator idempotency key.
    """
    try:
        # Inject W3C traceparent so SSE coordinator continues the trace.
        carrier: dict[str, str] = {}
        inject(carrier)

        kafka_event = StreamPartialEvent(
            event_id=event.event_id,
            correlation_id=correlation_id,
            tenant_id=event.tenant_id,
            traceparent=carrier.get("traceparent"),
            job_id=event.job_id,
            stream_event_type=event.event_type.value,
            stage=event.stage,
            stream_payload=event.payload,
            occurred_at=event.occurred_at,
        )

        await producer.producer.send_and_wait(
            Topic.STREAM_PARTIAL.value,
            key=event.job_id.encode(),
            value=kafka_event.model_dump_json().encode(),
        )
    except Exception as exc:
        # Best-effort: never let a stream event failure propagate to the caller.
        logger.warning(
            "stream_event_emit_failed",
            event_id=event.event_id,
            event_type=event.event_type,
            job_id=event.job_id,
            error=str(exc),
        )
