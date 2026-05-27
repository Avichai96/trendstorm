"""ReviewRepository Protocol.

Domain interface only — infrastructure/mongo/repositories/review_repository.py
is the concrete implementation.
"""
from __future__ import annotations

from typing import Protocol

from trendstorm.domain.reviews.models import ReviewDecision, ReviewRequest, ReviewStatus


class ReviewRepository(Protocol):
    async def insert(self, review: ReviewRequest) -> None: ...

    async def get(self, tenant_id: str, review_id: str) -> ReviewRequest | None: ...

    async def get_pending_for_job(
        self, tenant_id: str, job_id: str
    ) -> ReviewRequest | None: ...

    async def list_for_tenant(
        self,
        tenant_id: str,
        *,
        status: ReviewStatus | None = None,
        limit: int = 50,
        before_id: str | None = None,
    ) -> list[ReviewRequest]: ...

    async def resolve(
        self,
        tenant_id: str,
        review_id: str,
        *,
        decision: ReviewDecision,
        comment: str | None,
        reviewer_id: str | None,
    ) -> ReviewRequest | None: ...

    async def mark_timed_out(
        self, tenant_id: str, review_id: str
    ) -> ReviewRequest | None: ...

    async def list_expired_pending(self, *, limit: int = 100) -> list[ReviewRequest]: ...
