"""Reviews resource — HITL review queue operations.

Requires API key with the ``reviewer`` role.
"""

from __future__ import annotations

from trendstorm_shared.models import ReviewResponse
from trendstorm_shared.types import ReviewDecision, ReviewStatus

from ._base import AsyncAPIResource


class ReviewsResource(AsyncAPIResource):
    """Manage human-in-the-loop review decisions.

    All methods require an API key with the ``reviewer`` role.

    Examples::

        # List pending reviews
        pending = await ts.reviews.list_pending()
        for review in pending:
            print(review.job_id, review.timeout_at)

        # Approve a review
        resolved = await ts.reviews.approve(review.id)

        # Request refinement with feedback
        resolved = await ts.reviews.request_refinement(
            review.id, comment="Add more citations for the second insight."
        )
    """

    async def list(
        self,
        *,
        status: ReviewStatus | None = None,
        before_id: str | None = None,
        limit: int = 20,
    ) -> list[ReviewResponse]:
        """List reviews for the tenant, newest first."""
        data = await self._get(
            "/v1/reviews",
            status=status,
            before_id=before_id,
            limit=limit,
        )
        return [ReviewResponse.model_validate(r) for r in data]

    async def list_pending(self, *, limit: int = 20) -> list[ReviewResponse]:
        """Convenience: list only pending reviews."""
        return await self.list(status=ReviewStatus.PENDING, limit=limit)

    async def get(self, review_id: str) -> ReviewResponse:
        """Fetch a single review by ID."""
        data = await self._get(f"/v1/reviews/{review_id}")
        return ReviewResponse.model_validate(data)

    async def approve(
        self,
        review_id: str,
        *,
        comment: str | None = None,
    ) -> ReviewResponse:
        """Approve the analysis — it will proceed to publishing."""
        data = await self._post(
            f"/v1/reviews/{review_id}/resolve",
            {"decision": ReviewDecision.APPROVE, "comment": comment},
        )
        return ReviewResponse.model_validate(data)

    async def reject(
        self,
        review_id: str,
        *,
        comment: str | None = None,
    ) -> ReviewResponse:
        """Reject the analysis — job moves to REJECTED (terminal)."""
        data = await self._post(
            f"/v1/reviews/{review_id}/resolve",
            {"decision": ReviewDecision.REJECT, "comment": comment},
        )
        return ReviewResponse.model_validate(data)

    async def request_refinement(
        self,
        review_id: str,
        *,
        comment: str,
    ) -> ReviewResponse:
        """Ask the Analyst to re-run with the given feedback.

        ``comment`` is required and is injected as ``refinement_notes`` into
        the next Analyst pass.
        """
        data = await self._post(
            f"/v1/reviews/{review_id}/resolve",
            {"decision": ReviewDecision.REQUEST_REFINEMENT, "comment": comment},
        )
        return ReviewResponse.model_validate(data)
