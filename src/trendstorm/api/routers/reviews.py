"""Reviews router — HITL review queue endpoints.

GET  /v1/reviews?status=pending  List pending (or all) reviews for the tenant.
GET  /v1/reviews/{id}            Full detail for a single review.
POST /v1/reviews/{id}/resolve    Submit a reviewer decision (approve/reject/refine).

All routes require the "reviewer" role. The resolve endpoint writes the review
decision through the outbox pattern (Mongo-atomic write + outbox entry) rather
than publishing to Kafka directly, maintaining the same atomicity guarantee as
job creation.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from trendstorm.api.deps import MongoDep, SettingsDep
from trendstorm.domain.outbox.models import OutboxEntry
from trendstorm.domain.reviews.models import ReviewDecision, ReviewRequest, ReviewStatus
from trendstorm.infrastructure.mongo.repositories import MongoReviewRepository
from trendstorm.infrastructure.mongo.repositories._base import now_utc
from trendstorm.infrastructure.mongo.repositories.outbox_repository import (
    MongoOutboxRepository,
)
from trendstorm.orchestration.events import ReviewResolvedEvent
from trendstorm.orchestration.topics import Topic
from trendstorm.shared.errors import BusinessRuleError, NotFoundError
from trendstorm.shared.ids import new_id
from trendstorm.shared.logging import get_logger
from trendstorm.utils.headers_docs import require_role, require_tenant

logger = get_logger(__name__)

router = APIRouter(
    prefix="/v1/reviews",
    tags=["reviews"],
    dependencies=[require_tenant, require_role("reviewer")],
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    job_id: str
    analysis_id: str
    stage_under_review: str
    status: ReviewStatus
    reviewer_id: str | None
    decision_comment: str | None
    created_at: str
    resolved_at: str | None
    timeout_at: str
    sla_seconds: int

    @classmethod
    def from_domain(cls, r: ReviewRequest) -> "ReviewResponse":
        return cls(
            id=r.id,
            job_id=r.job_id,
            analysis_id=r.analysis_id,
            stage_under_review=r.stage_under_review,
            status=r.status,
            reviewer_id=r.reviewer_id,
            decision_comment=r.decision_comment,
            created_at=r.created_at.isoformat(),
            resolved_at=r.resolved_at.isoformat() if r.resolved_at else None,
            timeout_at=r.timeout_at.isoformat(),
            sla_seconds=r.sla_seconds,
        )


class ResolveReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: ReviewDecision
    comment: str | None = Field(
        default=None,
        max_length=2000,
        description="Required when decision=request_refinement; fed to the next analyst pass.",
    )


# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------

def _get_review_repo(mongo: MongoDep) -> MongoReviewRepository:
    return MongoReviewRepository(mongo)


def _get_outbox_repo(mongo: MongoDep) -> MongoOutboxRepository:
    return MongoOutboxRepository(mongo)


ReviewRepoDep = Annotated[MongoReviewRepository, Depends(_get_review_repo)]
OutboxRepoDep = Annotated[MongoOutboxRepository, Depends(_get_outbox_repo)]


def _tenant_id(request: Request) -> str:
    return request.state.tenant_id


def _principal_id(request: Request) -> str | None:
    """Return key_id or JWT subject for audit purposes."""
    ctx = getattr(request.state, "auth_context", None)
    if ctx is None:
        return None
    return ctx.key_id or ctx.subject


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("", summary="List reviews for the tenant")
async def list_reviews(
    request: Request,
    repo: ReviewRepoDep,
    status_filter: ReviewStatus | None = Query(default=None, alias="status"),
    before_id: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[ReviewResponse]:
    tenant_id = _tenant_id(request)
    reviews = await repo.list_for_tenant(
        tenant_id, status=status_filter, limit=limit, before_id=before_id
    )
    return [ReviewResponse.from_domain(r) for r in reviews]


@router.get("/{review_id}", summary="Get a single review by ID")
async def get_review(
    request: Request,
    repo: ReviewRepoDep,
    review_id: str = Path(...),
) -> ReviewResponse:
    tenant_id = _tenant_id(request)
    review = await repo.get(tenant_id, review_id)
    if review is None:
        raise NotFoundError(f"Review {review_id} not found")
    return ReviewResponse.from_domain(review)


@router.post("/{review_id}/resolve", status_code=status.HTTP_200_OK,
             summary="Submit a reviewer decision")
async def resolve_review(
    request: Request,
    repo: ReviewRepoDep,
    outbox_repo: OutboxRepoDep,
    mongo: MongoDep,
    body: ResolveReviewRequest,
    review_id: str = Path(...),
) -> ReviewResponse:
    tenant_id = _tenant_id(request)
    principal_id = _principal_id(request)

    review = await repo.get(tenant_id, review_id)
    if review is None:
        raise NotFoundError(f"Review {review_id} not found")
    if review.status != ReviewStatus.PENDING:
        raise BusinessRuleError(
            f"Review {review_id} is already {review.status.value}; cannot resolve again.",
            code="review_already_resolved",
        )
    if body.decision == ReviewDecision.REQUEST_REFINEMENT and not body.comment:
        raise BusinessRuleError(
            "comment is required when decision=request_refinement.",
            code="comment_required",
        )

    # Build the ReviewResolvedEvent for the outbox.
    resolved_event = ReviewResolvedEvent(
        correlation_id=new_id(),
        tenant_id=tenant_id,
        job_id=review.job_id,
        review_id=review.id,
        decision=body.decision.value,
        comment=body.comment,
        resolved_by=principal_id,
    )
    outbox_entry = OutboxEntry(
        tenant_id=tenant_id,
        topic=Topic.REVIEW_RESOLVED.value,
        key=review.job_id,
        payload=resolved_event.model_dump(mode="json"),
    )

    # Atomic: resolve review + insert outbox entry in a single Mongo transaction.
    async with await mongo.client.start_session() as session:
        async with session.start_transaction():
            updated = await repo.resolve(
                tenant_id,
                review_id,
                decision=body.decision,
                comment=body.comment,
                reviewer_id=principal_id,
            )
            if updated is None:
                # Race: another request resolved it between our get and resolve.
                raise BusinessRuleError(
                    f"Review {review_id} was concurrently resolved.",
                    code="review_already_resolved",
                )
            await outbox_repo.insert(outbox_entry)

    logger.info(
        "review.resolved",
        review_id=review_id,
        job_id=review.job_id,
        decision=body.decision.value,
        resolved_by=principal_id,
        tenant_id=tenant_id,
    )
    return ReviewResponse.from_domain(updated)
