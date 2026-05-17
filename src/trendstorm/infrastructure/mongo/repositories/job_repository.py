"""MongoDB implementation of JobRepository.

Implements the `JobRepository` Protocol from `domain/jobs/repository.py`.
Public method signatures match the Protocol exactly so the Protocol's
structural typing kicks in — no inheritance required.

All queries are routed through `self._tenant_query()` from the mixin,
guaranteeing tenant scope. Even a `get(tenant, job_id)` includes
`tenant_id` in the filter — never just `_id`.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from pymongo.errors import PyMongoError

from trendstorm.domain.jobs.models import Job
from trendstorm.infrastructure.mongo.repositories._base import (
    TenantScopedRepository,
    now_utc,
    raise_db_error,
)
from trendstorm.infrastructure.mongo.schema import Collection
from trendstorm.shared.logging import get_logger
from trendstorm.shared.types import JobStatus

if TYPE_CHECKING:
    from datetime import datetime


logger = get_logger(__name__)


class MongoJobRepository(TenantScopedRepository[Job]):
    """Concrete JobRepository backed by MongoDB."""

    collection: ClassVar[Collection] = Collection.JOBS
    model: ClassVar[type[Job]] = Job

    # ----------------------------------------------------------------- #
    # JobRepository protocol implementation                             #
    # ----------------------------------------------------------------- #

    async def insert(self, job: Job, *, session: object | None = None) -> None:
        await self._insert(self._encode(job), what=f"Job {job.id}", session=session)

    async def get(self, tenant_id: str, job_id: str) -> Job | None:
        doc = await self._find_one(
            self._tenant_query(tenant_id, _id=job_id),
            what=f"Job {job_id}",
        )
        return self._decode(doc) if doc else None

    async def update_status(
        self,
        tenant_id: str,
        job_id: str,
        status: JobStatus,
        *,
        failure_code: str | None = None,
        failure_message: str | None = None,
    ) -> None:
        """Atomically transition a job's status.

        Always sets `updated_at`. Sets `completed_at` if the new status is
        terminal. Failure fields are set only when provided — leaving them
        unset is meaningful (vs explicitly clearing them).
        """
        update: dict[str, dict[str, Any]] = {"$set": {"status": status.value, "updated_at": now_utc()}}
        if status.is_terminal:
            update["$set"]["completed_at"] = now_utc()
        if failure_code is not None:
            update["$set"]["failure_code"] = failure_code
        if failure_message is not None:
            update["$set"]["failure_message"] = failure_message

        try:
            result = await self._coll.update_one(
                self._tenant_query(tenant_id, _id=job_id), update
            )
        except PyMongoError as e:
            raise_db_error(e, operation="update_status", job_id=job_id)

        if result.matched_count == 0:
            # Either the job doesn't exist or the tenant scope rejected it.
            # We log but don't raise — callers (e.g. the worker reconciling
            # state) shouldn't crash because the job was deleted by TTL.
            logger.warning("update_status_no_match", job_id=job_id, status=status.value)

    async def list_by_tenant(
        self,
        tenant_id: str,
        *,
        status: JobStatus | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[Job], str | None]:
        """Cursor-paginated list, newest first.

        Cursor semantics: `cursor` is the last-seen `_id`. We page by
        `_id < cursor` because ULIDs sort lexicographically by creation
        time. This is O(1) per page (vs offset which is O(N)) and stable
        under concurrent inserts (a new job inserted between pages doesn't
        shift the page boundaries).
        """
        query: dict[str, object] = self._tenant_query(tenant_id)
        if status is not None:
            query["status"] = status.value
        if cursor is not None:
            query["_id"] = {"$lt": cursor}

        # Fetch limit+1 to detect "has more" without an extra round trip.
        docs = await self._find_many(
            query,
            sort=[("_id", -1)],
            limit=limit + 1,
            what="jobs list",
        )

        has_more = len(docs) > limit
        docs = docs[:limit]
        jobs = [self._decode(d) for d in docs]
        next_cursor = jobs[-1].id if has_more and jobs else None
        return jobs, next_cursor

    # ----------------------------------------------------------------- #
    # Aggregations (read-only analytics)                                #
    # ----------------------------------------------------------------- #

    async def avg_duration_by_category(
        self,
        tenant_id: str,
        *,
        since: datetime,
    ) -> list[dict[str, object]]:
        """Avg duration of COMPLETED jobs per category since `since`.

        This is the aggregation discussed in the Phase 4/5 teaser. Notes
        on how it's intended to execute:

        - The `$match` stage MUST be first. It uses the index
          `(tenant_id, status, created_at)` to:
            - reject other tenants at the index level (huge);
            - reject non-completed statuses at the index level;
            - skip to the right `created_at` range.
          A `db.jobs.aggregate(...).explain()` output should show
          IXSCAN, not COLLSCAN, on this stage.

        - `$group` reduces to one row per category. Mongo streams the
          input — it doesn't materialize the whole match result. Memory
          is O(num_categories), not O(num_jobs).

        - `$project` shapes the output. Cheap.

        Why aggregation in Mongo vs in app code? Imagine 1M jobs match.
        Pulling 1M docs over the network to Python costs ~50 seconds of
        bandwidth alone. The aggregation returns ~50 rows (one per
        category). Network: 5KB total. Latency: ~100ms with the index.

        Where this approach FAILS:
            - The result has 100k+ groups (cardinality of `category_id`
              is huge). Memory pressure on Mongo.
            - The query window is unbounded ("all time"). Worse, no
              `since` filter at all.
            - There's no index supporting the $match stage. Then it's a
              full collection scan, and you've moved the pain from app
              to database.

        For sustained dashboard load, pre-aggregate via $merge into a
        rollup collection (Phase 11).
        """
        pipeline: list[dict[str, Any]] = [
            {
                "$match": {
                    "tenant_id": tenant_id,
                    "status": JobStatus.COMPLETED.value,
                    "completed_at": {"$gte": since},
                }
            },
            {
                # Compute duration on-the-fly from completed_at - created_at.
                # In a hot path we'd persist this as a field at completion
                # time to avoid the $subtract per row, but at our scale this
                # is fine.
                "$addFields": {
                    "duration_seconds": {
                        "$divide": [
                            {"$subtract": ["$completed_at", "$created_at"]},
                            1000,    # ms -> s
                        ]
                    }
                }
            },
            {
                "$group": {
                    "_id": "$category_id",
                    "avg_duration_seconds": {"$avg": "$duration_seconds"},
                    "job_count": {"$sum": 1},
                }
            },
            {
                "$project": {
                    "category_id": "$_id",
                    "avg_duration_seconds": 1,
                    "job_count": 1,
                    "_id": 0,
                }
            },
            {"$sort": {"job_count": -1}},
        ]
        try:
            cursor = self._coll.aggregate(pipeline)
            return await cursor.to_list(length=None)
        except PyMongoError as e:
            raise_db_error(e, operation="avg_duration_by_category", tenant_id=tenant_id)
            return []  # unreachable
