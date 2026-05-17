"""MongoDB implementation of OutboxRepository.

The outbox pattern closes the persist-before-publish window: JobService
wraps (jobs.insert + outbox.insert) in a single Mongo transaction. This
worker polls `find_pending` on every tick and publishes to Kafka.

All queries are tenant-scoped via `_tenant_query()` from the mixin, matching
the pattern of all other repositories. The relay worker queries ALL tenants
in a single scan, but each entry carries tenant_id for downstream routing.

The hot query is the partial-index scan on `published_at=None` ordered by
`created_at` — it touches only unpublished entries, which is tiny on a
healthy system.
"""
from __future__ import annotations

from typing import ClassVar

from pymongo.errors import DuplicateKeyError, PyMongoError

from trendstorm.domain.outbox.models import OutboxEntry
from trendstorm.infrastructure.mongo.repositories._base import (
    TenantScopedRepository,
    now_utc,
    raise_db_error,
    raise_on_dup_key,
)
from trendstorm.infrastructure.mongo.schema import Collection
from trendstorm.shared.logging import get_logger

logger = get_logger(__name__)


class MongoOutboxRepository(TenantScopedRepository[OutboxEntry]):
    """Concrete OutboxRepository backed by MongoDB."""

    collection: ClassVar[Collection] = Collection.OUTBOX
    model: ClassVar[type[OutboxEntry]] = OutboxEntry

    async def insert(self, entry: OutboxEntry, *, session: object | None = None) -> None:
        """Persist a pending outbox entry, optionally inside a Mongo session."""
        try:
            await self._insert(
                self._encode(entry),
                what=f"OutboxEntry {entry.id}",
                session=session,
            )
        except DuplicateKeyError as e:
            raise_on_dup_key(e, what=f"OutboxEntry {entry.id}")

    async def find_pending(self, *, limit: int = 100) -> list[OutboxEntry]:
        """Return unpublished entries ordered oldest-first.

        Deliberately cross-tenant: the relay worker processes all tenants.
        Does NOT use _tenant_query — outbox relay has no tenant context.
        """
        try:
            cursor = self._coll.find(
                {"published_at": None},
                sort=[("created_at", 1)],
                limit=limit,
            )
            docs = await cursor.to_list(length=limit)
        except PyMongoError as e:
            raise_db_error(e, operation="outbox.find_pending")

        return [self._decode(doc) for doc in docs]

    async def mark_published(self, entry_id: str) -> None:
        """Stamp published_at=now() after Kafka ack."""
        try:
            await self._coll.update_one(
                {"_id": entry_id},
                {"$set": {"published_at": now_utc()}},
            )
        except PyMongoError as e:
            raise_db_error(e, operation="outbox.mark_published", entry_id=entry_id)

    async def increment_retry(self, entry_id: str) -> int:
        """Atomically bump retry_count; return the new value."""
        try:
            result = await self._coll.find_one_and_update(
                {"_id": entry_id},
                {"$inc": {"retry_count": 1}},
                return_document=True,
                projection={"retry_count": 1},
            )
        except PyMongoError as e:
            raise_db_error(e, operation="outbox.increment_retry", entry_id=entry_id)

        if result is None:
            logger.warning("outbox.increment_retry_no_match", entry_id=entry_id)
            return 0
        return int(result.get("retry_count", 0))
