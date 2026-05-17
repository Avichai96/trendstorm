"""Redis Pub/Sub wrapper for live SSE fanout.

Architecture role:
    Workers → Kafka stream.partial.v1 → SSE Coordinator
    SSE Coordinator → PUBLISH to Redis channel → connected SSE endpoints

Channel naming: {prefix}:{job_id}:live
Each job has its own channel so unrelated SSE connections don't receive
each other's events. Channel names are derived from job_id; they are
created implicitly on first PUBLISH and cleaned up when all subscribers
disconnect (Redis Pub/Sub channels have no persistence and no TTL — they
exist only while someone is subscribed or during PUBLISH).

Why not use Kafka directly from the SSE endpoint?
    Kafka consumers are heavy (group rebalance, offset management). For
    ephemeral HTTP connections with short lifetimes, Redis Pub/Sub is the
    right tool: no offset state, no group bookkeeping, instant delivery.
    Durability is handled by Redis Streams (XADD side) — Pub/Sub is just
    the live-notification layer on top.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

from trendstorm.shared.config import SSESettings
from trendstorm.shared.errors import DatabaseError
from trendstorm.shared.logging import get_logger

logger = get_logger(__name__)

_CHANNEL_TPL = "{prefix}:{job_id}:live"


class RedisPubSub:
    """Live-fanout pub/sub for per-job SSE streams.

    Like RedisStreamStore, this class does NOT own a Redis client.
    Call init() with the live redis-py async client before using.
    """

    def __init__(self, settings: SSESettings) -> None:
        self._settings = settings
        self._client: Any = None

    def init(self, redis_client: Any) -> None:
        """Inject the live redis-py async client."""
        self._client = redis_client

    def channel(self, job_id: str) -> str:
        """Return the pub/sub channel name for a given job."""
        return _CHANNEL_TPL.format(prefix=self._settings.channel_prefix, job_id=job_id)

    async def publish(self, job_id: str, payload: dict[str, Any]) -> int:
        """Publish payload to the job channel.

        Returns the number of subscribers that received the message.
        0 is normal when no SSE clients are connected (fire-and-forget).
        """
        if self._client is None:
            raise DatabaseError("RedisPubSub not initialised; call init() first")
        channel = self.channel(job_id)
        receivers: int = await self._client.publish(channel, json.dumps(payload))
        return receivers

    async def subscribe(
        self,
        job_id: str,
        *,
        stop_event: asyncio.Event | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Return an async iterator that yields decoded payloads from the job channel.

        Exits when stop_event fires, when `aclose()` is called, or when
        the caller breaks out of the loop.

        Usage:
            async for payload in await pubsub.subscribe(job_id, stop_event=done):
                ...  # break when terminal event seen
        """
        if self._client is None:
            raise DatabaseError("RedisPubSub not initialised; call init() first")

        return _PubSubIterator(
            client=self._client,
            channel=self.channel(job_id),
            stop_event=stop_event,
            job_id=job_id,
        )


class _PubSubIterator:
    """Async iterator backed by a single Redis Pub/Sub subscription.

    stop_event support: when a stop_event is provided, __anext__ races
    the Redis listener against the event using asyncio.wait so that
    setting the event wakes the iterator even when no message arrives.
    """

    def __init__(
        self,
        *,
        client: Any,
        channel: str,
        stop_event: asyncio.Event | None,
        job_id: str,
    ) -> None:
        self._client = client
        self._channel = channel
        self._stop_event = stop_event
        self._job_id = job_id
        self._pubsub: Any = None
        self._listener: Any = None
        self._closed = False

    def __aiter__(self) -> _PubSubIterator:
        return self

    async def _ensure_subscribed(self) -> None:
        if self._pubsub is None:
            self._pubsub = self._client.pubsub()
            await self._pubsub.subscribe(self._channel)
            self._listener = self._pubsub.listen()
            logger.debug("pubsub_subscribed", job_id=self._job_id, channel=self._channel)

    async def __anext__(self) -> dict[str, Any]:
        if self._closed:
            raise StopAsyncIteration

        await self._ensure_subscribed()

        while True:
            if self._stop_event and self._stop_event.is_set():
                await self._cleanup()
                raise StopAsyncIteration

            # Race: next message vs stop_event (if present) so callers don't
            # block indefinitely when no messages arrive after stop.
            if self._stop_event:
                listen_task = asyncio.ensure_future(self._listener.__anext__())
                stop_task = asyncio.ensure_future(self._stop_event.wait())
                done, pending = await asyncio.wait(
                    [listen_task, stop_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                    with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                        await t

                if stop_task in done and stop_task not in pending:
                    # stop_event fired; cancel listen and exit
                    await self._cleanup()
                    raise StopAsyncIteration

                try:
                    raw = listen_task.result()
                except StopAsyncIteration:
                    await self._cleanup()
                    raise
            else:
                raw = await self._listener.__anext__()

            if raw["type"] != "message":
                continue

            try:
                payload: dict[str, Any] = json.loads(raw["data"])
                return payload
            except (json.JSONDecodeError, TypeError) as exc:
                logger.warning("pubsub_bad_message", error=str(exc))
                continue

    async def _cleanup(self) -> None:
        if self._pubsub is not None and not self._closed:
            self._closed = True
            try:
                await self._pubsub.unsubscribe(self._channel)
                await self._pubsub.aclose()
            except Exception:  # noqa: S110  # best-effort cleanup; finally block clears state
                pass
            finally:
                self._pubsub = None
            logger.debug("pubsub_unsubscribed", job_id=self._job_id, channel=self._channel)

    async def aclose(self) -> None:
        """Explicit cleanup for callers that don't exhaust the iterator."""
        self._closed = True
        await self._cleanup()
