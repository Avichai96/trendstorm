"""Idempotency repository.

Kafka delivers at-least-once. Our agents do expensive work (LLM calls, blob
writes). We need IDEMPOTENCY to make duplicate deliveries safe.

Pattern: every Kafka message handler does this BEFORE doing work:

    1. Compute idempotency_key from message content (job_id + stage + attempt).
    2. Try to insert a marker doc with `key` as unique _id.
    3. If insert succeeds:  proceed with work.
       If duplicate key:    a prior delivery already did this work -> skip.
    4. On success, update the marker with the result.

Why a separate collection (`idempotency`) and not a flag on the job doc?
    - Per-stage granularity: a job has multiple keys (one per attempt per stage).
    - TTL: idempotency markers expire after the message can no longer be
      redelivered (we set TTL = 24h, matching Kafka retention).
    - Smaller hot collection: high-frequency reads/writes don't bloat `jobs`.

Race condition handling:
    Two consumers might process the same message simultaneously. Both call
    `acquire(key)`. Mongo's unique index ensures only one succeeds; the other
    gets `DuplicateKeyError` -> returns False -> skips. This is the
    "compare-and-swap" pattern, atomic and scale-safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from pymongo.errors import DuplicateKeyError, PyMongoError

from trendstorm.infrastructure.mongo.client import MongoClient
from trendstorm.infrastructure.mongo.repositories._base import now_utc
from trendstorm.infrastructure.mongo.schema import Collection
from trendstorm.shared.errors import DatabaseError
from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    from motor.motor_asyncio import AsyncIOMotorCollection


logger = get_logger(__name__)

# How long do we remember a key? Should exceed Kafka retention so a late
# redelivery can't cause re-processing after the marker expired.
DEFAULT_TTL_HOURS = 48


@dataclass(frozen=True, slots=True)
class IdempotencyResult:
    """Result of acquire().

    `acquired=False` means another consumer won the race or a previous
    delivery already processed this key.
    """

    acquired: bool
    existing_result: dict[str, Any] | None = None


class IdempotencyRepository:
    """Mongo-backed idempotency markers with TTL.

    Collection has these indexes (created at startup):
        - {_id: 1}                       primary
        - {expires_at: 1} TTL            auto-cleanup
    """

    def __init__(self, mongo: MongoClient, ttl_hours: int = DEFAULT_TTL_HOURS) -> None:
        self._mongo = mongo
        self._ttl = timedelta(hours=ttl_hours)

    @property
    def _coll(self) -> AsyncIOMotorCollection:  # type: ignore[type-arg]  # motor stubs lack precise generic params
        return self._mongo.db[Collection.IDEMPOTENCY]

    @staticmethod
    def make_key(job_id: str, stage: str, attempt: int) -> str:
        """Canonical key format: `{job_id}:{stage}:{attempt}`.

        Including `attempt` is critical — a retry IS a new logical operation
        and must get its own idempotency key. If we used only `job_id:stage`,
        retries would always be deduplicated as "already done."
        """
        return f"{job_id}:{stage}:{attempt}"

    async def acquire(self, key: str) -> IdempotencyResult:
        """Atomically claim a key. Returns (acquired=True) if we won the race.

        On `acquired=False`, the caller MUST skip the work — another consumer
        is already handling (or has handled) this operation.
        """
        now = now_utc()
        expires = now + self._ttl
        doc = {
            "_id": key,
            "created_at": now,
            "expires_at": expires,
            "status": "in_progress",
        }
        try:
            await self._coll.insert_one(doc)
        except DuplicateKeyError:
            # Another consumer holds (or held) this key. Fetch it to surface
            # any result we should reuse.
            existing = await self._coll.find_one({"_id": key})
            return IdempotencyResult(acquired=False, existing_result=existing)
        except PyMongoError as e:
            raise DatabaseError(
                "Idempotency acquire failed",
                context={"key": key, "error": str(e)},
            ) from e

        return IdempotencyResult(acquired=True)

    async def complete(self, key: str, result: dict[str, Any] | None = None) -> None:
        """Mark a key as completed and store the result."""
        update: dict[str, Any] = {"$set": {"status": "completed", "completed_at": now_utc()}}
        if result is not None:
            update["$set"]["result"] = result
        try:
            await self._coll.update_one({"_id": key}, update)
        except PyMongoError as e:
            # Best-effort — even if we fail to mark completion, the key will
            # expire via TTL. Worst case: a retry within TTL re-acquires
            # successfully because the prior consumer holds an `in_progress`
            # marker that the dup-key check sees but we'd still need to skip
            # based on `status`. We log and continue.
            logger.warning("idempotency_complete_failed", key=key, error=str(e))

    async def release(self, key: str) -> None:
        """Release a key for retry (called on transient failure).

        Without this, a transient error during in-progress work would leave
        the marker permanently `in_progress`, blocking all retries until TTL.
        """
        try:
            await self._coll.delete_one({"_id": key, "status": "in_progress"})
        except PyMongoError as e:
            logger.warning("idempotency_release_failed", key=key, error=str(e))
