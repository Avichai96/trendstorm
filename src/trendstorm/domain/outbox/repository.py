"""OutboxRepository protocol."""
from __future__ import annotations

from typing import Protocol

from trendstorm.domain.outbox.models import OutboxEntry


class OutboxRepository(Protocol):
    """Persistence contract for outbox entries."""

    async def insert(self, entry: OutboxEntry, *, session: object | None = None) -> None:
        """Persist a new pending entry.

        `session` is an opaque handle for transactional callers (e.g. JobService
        wrapping job+outbox in one Mongo transaction). Ignored by non-transactional
        implementations.
        """
        ...

    async def find_pending(self, *, limit: int = 100) -> list[OutboxEntry]:
        """Return unpublished entries ordered by created_at ascending.

        The relay worker polls this on every tick. `limit` caps the batch so
        one stuck entry cannot block the relay for an unbounded time.
        """
        ...

    async def mark_published(self, entry_id: str) -> None:
        """Stamp published_at=now(). The relay calls this after Kafka ack."""
        ...

    async def increment_retry(self, entry_id: str) -> int:
        """Atomically increment retry_count; return the new value.

        Called when Kafka publish fails so the relay can apply backoff and
        the ops team can query for stuck entries.
        """
        ...
