r"""SSE event generator service.

Implements the subscribe-before-read pattern to prevent race conditions
between job stream replay and live pub/sub delivery:

    1. SUBSCRIBE to Pub/Sub channel first (before reading from Streams).
    2. XRANGE the Redis Stream to replay history up to last_event_id.
    3. Tail live Pub/Sub for events not yet in the stream.
    4. Yield SSE heartbeat comments on the configured interval.

The race condition this prevents:
    Without subscribe-first, new events published between XRANGE and
    SUBSCRIBE would be silently lost. With subscribe-first, events
    published after the SUBSCRIBE but before XRANGE get buffered in the
    Pub/Sub channel; after replay they are deduplicated by seq number.

SSE wire format (W3C EventSource spec):
    id: <seq>\\n
    event: <event_type>\\n
    data: <json>\\n
    \\n
    (blank line terminates each event)

Heartbeats are SSE comments (": heartbeat\\n\\n") — browsers keep the
connection alive but don't fire message events.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from opentelemetry import trace

from trendstorm.domain.streaming.events import StreamEventType
from trendstorm.infrastructure.redis.pubsub import RedisPubSub
from trendstorm.infrastructure.redis.streams import RedisStreamStore
from trendstorm.shared.config import SSESettings
from trendstorm.shared.logging import get_logger

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)


def _format_sse(payload: dict[str, Any]) -> str:
    """Format a payload dict as a W3C SSE string."""
    seq = payload.get("seq", 0)
    event_type = payload.get("event_type", "message")
    return (
        f"id: {seq}\n"
        f"event: {event_type}\n"
        f"data: {json.dumps(payload)}\n"
        "\n"
    )


def _heartbeat() -> str:
    """SSE comment used as a keep-alive (doesn't trigger message on client)."""
    return ": heartbeat\n\n"


def _is_terminal(payload: dict[str, Any]) -> bool:
    event_type = payload.get("event_type", "")
    try:
        return StreamEventType(event_type).is_terminal
    except ValueError:
        return False


async def sse_event_generator(
    job_id: str,
    *,
    stream_store: RedisStreamStore,
    pubsub: RedisPubSub,
    settings: SSESettings,
    last_event_id: int = 0,
) -> AsyncIterator[str]:
    """Yield SSE-formatted strings for a single job stream connection.

    Args:
        job_id: The job whose events to stream.
        stream_store: Provides XRANGE history replay.
        pubsub: Provides live Pub/Sub tail.
        settings: SSESettings (heartbeat interval, channel config).
        last_event_id: Client Last-Event-ID header (0 = start from beginning).

    Yields formatted SSE strings. The generator closes itself when a
    terminal event (REPORT_READY or JOB_FAILED) arrives.

    """
    stop_event = asyncio.Event()
    seen_seqs: set[int] = set()
    span = tracer.start_span("sse.stream", attributes={"job_id": job_id})

    # Step 1: SUBSCRIBE before reading history (prevents the mid-read race).
    live_iter = await pubsub.subscribe(job_id, stop_event=stop_event)

    try:
        # Step 2: Replay history — events the client missed before connecting.
        with tracer.start_as_current_span("sse.replay"):
            history = await stream_store.read_from(job_id, min_seq=last_event_id)

        for payload in history:
            seq = payload.get("seq", 0)
            if seq in seen_seqs:
                continue
            seen_seqs.add(seq)
            yield _format_sse(payload)
            if _is_terminal(payload):
                return

        # Step 3: Tail live events from Pub/Sub, interspersed with heartbeats.
        heartbeat_interval = settings.heartbeat_seconds
        last_heartbeat_at = asyncio.get_event_loop().time()

        with tracer.start_as_current_span("sse.live_tail"):
            async for payload in live_iter:
                seq = payload.get("seq", 0)
                if seq in seen_seqs:
                    continue
                seen_seqs.add(seq)

                now = asyncio.get_event_loop().time()
                if now - last_heartbeat_at >= heartbeat_interval:
                    yield _heartbeat()
                    last_heartbeat_at = now

                yield _format_sse(payload)

                if _is_terminal(payload):
                    return

    finally:
        stop_event.set()
        await live_iter.aclose()  # type: ignore[attr-defined]
        span.end()
