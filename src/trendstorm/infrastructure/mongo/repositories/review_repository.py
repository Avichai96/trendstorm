"""MongoDB implementation of ReviewRepository."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from pymongo import ASCENDING, DESCENDING

from trendstorm.domain.reviews.models import ReviewDecision, ReviewRequest, ReviewStatus
from trendstorm.infrastructure.mongo.repositories._base import (
    TenantScopedRepository,
    now_utc,
)
from trendstorm.infrastructure.mongo.schema import Collection
from trendstorm.shared.logging import get_logger

logger = get_logger(__name__)


class MongoReviewRepository(TenantScopedRepository[ReviewRequest]):
    collection: ClassVar[Collection] = Collection.REVIEWS
    model: ClassVar[type[ReviewRequest]] = ReviewRequest

    async def insert(self, review: ReviewRequest) -> None:
        await self._insert(self._encode(review), what=f"ReviewRequest {review.id}")

    async def get(self, tenant_id: str, review_id: str) -> ReviewRequest | None:
        doc = await self._find_one(
            self._tenant_query(tenant_id, _id=review_id),
            what=f"ReviewRequest {review_id}",
        )
        return self._decode(doc) if doc else None

    async def get_pending_for_job(self, tenant_id: str, job_id: str) -> ReviewRequest | None:
        doc = await self._find_one(
            self._tenant_query(tenant_id, job_id=job_id, status=ReviewStatus.PENDING),
            what=f"pending ReviewRequest for job {job_id}",
        )
        return self._decode(doc) if doc else None

    async def list_for_tenant(
        self,
        tenant_id: str,
        *,
        status: ReviewStatus | None = None,
        limit: int = 50,
        before_id: str | None = None,
    ) -> list[ReviewRequest]:
        query = self._tenant_query(tenant_id)
        if status is not None:
            query["status"] = status.value
        if before_id is not None:
            query["_id"] = {"$lt": before_id}
        docs = await self._find_many(
            query,
            sort=[("_id", DESCENDING)],
            limit=limit,
            what="ReviewRequest list",
        )
        return [self._decode(d) for d in docs]

    async def resolve(
        self,
        tenant_id: str,
        review_id: str,
        *,
        decision: ReviewDecision,
        comment: str | None,
        reviewer_id: str | None,
    ) -> ReviewRequest | None:
        status_map = {
            ReviewDecision.APPROVE: ReviewStatus.APPROVED,
            ReviewDecision.REJECT: ReviewStatus.REJECTED,
            ReviewDecision.REQUEST_REFINEMENT: ReviewStatus.REFINEMENT_REQUESTED,
        }
        new_status = status_map[decision]
        update: dict[str, object] = {
            "$set": {
                "status": new_status.value,
                "reviewer_id": reviewer_id,
                "decision_comment": comment,
                "resolved_at": now_utc(),
            }
        }
        doc = await self._coll.find_one_and_update(
            self._tenant_query(tenant_id, _id=review_id, status=ReviewStatus.PENDING),
            update,
            return_document=True,
        )
        return self._decode(doc) if doc else None

    async def mark_timed_out(self, tenant_id: str, review_id: str) -> ReviewRequest | None:
        doc = await self._coll.find_one_and_update(
            self._tenant_query(tenant_id, _id=review_id, status=ReviewStatus.PENDING),
            {
                "$set": {
                    "status": ReviewStatus.TIMED_OUT.value,
                    "resolved_at": now_utc(),
                }
            },
            return_document=True,
        )
        return self._decode(doc) if doc else None

    async def list_expired_pending(self, *, limit: int = 100) -> list[ReviewRequest]:
        """Cross-tenant sweeper query. Not scoped by tenant_id intentionally."""
        now = datetime.now(UTC)
        docs = await self._find_many(
            {"status": ReviewStatus.PENDING.value, "timeout_at": {"$lte": now}},
            sort=[("timeout_at", ASCENDING)],
            limit=limit,
            what="expired ReviewRequest list",
        )
        return [self._decode(d) for d in docs]
