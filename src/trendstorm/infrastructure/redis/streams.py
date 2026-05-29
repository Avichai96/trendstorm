"""Redis Streams wrapper for per-job SSE event logs.

Each job gets a dedicated stream key: stream:{job_id}:events
Events are written via XADD (append-only) and read via XRANGE.
The stream TTL is reset on every write (24h by default) so active
streams survive; idle ones expire automatically.

Why XADD/XRANGE over a Redis List?
    - Streams support range queries by ID so Last-Event-ID resumption
      works without reading from the beginning every time.
    - Redis Streams are append-only by design — fits the event-log pattern.
    - Entries can carry multiple fields in one command (no JSON wrapping needed).
"""

from __future__ import annotations

import json
from typing import Any

from trendstorm.shared.config import SSESettings
from trendstorm.shared.errors import DatabaseError
from trendstorm.shared.logging import get_logger

logger = get_logger(__name__)

_STREAM_KEY_TPL = "{prefix}:{job_id}:events"
_SEQ_KEY_TPL = "{prefix}:{job_id}:seq"


class RedisStreamStore:
    """Append-only per-job event log backed by Redis Streams.

    This class does NOT own a Redis client — it accepts the redis-py
    async client from `RedisClient.client` so the lifecycle is managed
    by the application (connected once in lifespan, shared across users).

    All XADD entries carry a single JSON field named "data" to keep the
    read path simple: callers get a list of dicts when reading, and
    ``data`` is always the key to deserialise.
    """

    def __init__(self, settings: SSESettings) -> None:
        self._settings = settings
        self._client: Any = None  # redis.asyncio.Redis, injected via init()

    def init(self, redis_client: Any) -> None:
        """Inject the live redis-py async client. Called after connect()."""
        self._client = redis_client

    # -------------------------------------------------------------------------
    # Write path
    # -------------------------------------------------------------------------

    async def append(self, job_id: str, payload: dict[str, Any]) -> str:
        """XADD the payload to the job stream.

        Returns the Redis stream entry ID (e.g. "1685000000000-0").
        Sets (or refreshes) the stream TTL so idle streams expire.
        """
        if self._client is None:
            raise DatabaseError("RedisStreamStore not initialised; call init() first")

        key = _STREAM_KEY_TPL.format(prefix=self._settings.channel_prefix, job_id=job_id)
        ttl_seconds = self._settings.event_log_ttl_hours * 3600

        entry_id: str = await self._client.xadd(key, {"data": json.dumps(payload)})
        await self._client.expire(key, ttl_seconds)
        return entry_id

    async def incr_seq(self, job_id: str) -> int:
        """Atomically increment and return the job-scoped seq counter."""
        if self._client is None:
            raise DatabaseError("RedisStreamStore not initialised; call init() first")

        seq_key = _SEQ_KEY_TPL.format(prefix=self._settings.channel_prefix, job_id=job_id)
        ttl_seconds = self._settings.event_log_ttl_hours * 3600
        seq: int = await self._client.incr(seq_key)
        # Co-expire the seq counter with the stream so both clean up together.
        await self._client.expire(seq_key, ttl_seconds)
        return seq

    # -------------------------------------------------------------------------
    # Read path
    # -------------------------------------------------------------------------

    async def read_from(
        self,
        job_id: str,
        *,
        min_seq: int = 0,
    ) -> list[dict[str, Any]]:
        """Return all stored events with seq >= min_seq, in order.

        Events are stored as {"data": "<json>", "seq": "<int>"}.
        We filter on the decoded seq field so that min_seq=0 returns
        everything and min_seq=N implements Last-Event-ID resumption.

        Returns a list of decoded payload dicts.
        """
        if self._client is None:
            raise DatabaseError("RedisStreamStore not initialised; call init() first")

        key = _STREAM_KEY_TPL.format(prefix=self._settings.channel_prefix, job_id=job_id)
        # XRANGE - returns all entries as [(id, {field: value, ...}), ...]
        raw: list[tuple[str, dict[str, str]]] = await self._client.xrange(key)

        result: list[dict[str, Any]] = []
        for _entry_id, fields in raw:
            payload = json.loads(fields["data"])
            seq = payload.get("seq", 0)
            if seq >= min_seq:
                result.append(payload)
        return result

    async def stream_key(self, job_id: str) -> str:
        """Return the Redis key for the job stream (for subscribe-before-read)."""
        return _STREAM_KEY_TPL.format(prefix=self._settings.channel_prefix, job_id=job_id)
