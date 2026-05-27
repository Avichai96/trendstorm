# HITL Reviews

TrendStorm's Human-in-the-Loop (HITL) system lets tenants gate analysis publication
behind human approval. This is configured per-tenant in the server admin interface.

## Overview

When HITL is enabled for a tenant:

1. After analysis completes, the orchestrator evaluates whether to flag it for review.
2. If flagged, the job pauses at `AWAITING_REVIEW` and emits a `review_required` SSE event.
3. A reviewer approves, rejects, or requests refinement via the reviews API.
4. The job resumes (or terminates with `REJECTED`).

## Reviewer role

All review endpoints require an API key with the `reviewer` role.
Keys get this role when created with it server-side.

## Listing pending reviews

```python
pending = await ts.reviews.list_pending(limit=20)
for review in pending:
    print(
        review.id,
        review.job_id,
        f"{review.sla_seconds / 3600:.0f}h SLA",
        review.timeout_at.isoformat(),
    )
```

Or with explicit status filter:

```python
from trendstorm_shared.types import ReviewStatus

all_reviews = await ts.reviews.list(status=ReviewStatus.PENDING, limit=50)
```

## Actioning a review

### Approve

The analysis proceeds to publishing:

```python
resolved = await ts.reviews.approve(review_id)
print(resolved.status)  # approved
```

### Reject

The job moves to `REJECTED` (terminal):

```python
resolved = await ts.reviews.reject(
    review_id,
    comment="Analysis quality too low — too many unsupported claims.",
)
```

### Request refinement

The Analyst re-runs with the comment as guidance:

```python
resolved = await ts.reviews.request_refinement(
    review_id,
    comment="Add more citations for claims about model scaling. The second insight needs empirical backing.",
)
```

!!! warning
    `comment` is **required** for `request_refinement`. The server rejects the request with 400 if omitted.

## SLA and auto-reject

Each review has a configured SLA (default 48 hours). If no reviewer acts before
`timeout_at`, the sweeper auto-rejects the review and the job moves to `REJECTED`.

The `PendingReviewsAgingHigh` alert fires at 80% of SLA (38.4 hours) to give
reviewers time to act. See [ops/runbooks/review-aging.md](../../../ops/runbooks/review-aging.md).

## Full reviewer workflow example

```python
import asyncio
from trendstorm_sdk import TrendStormClient

async def process_queue():
    async with TrendStormClient(api_key="ts_live_reviewer_...") as ts:
        pending = await ts.reviews.list_pending()
        for review in pending:
            print(f"Review {review.id} for job {review.job_id}")
            # Your evaluation logic here:
            score = evaluate_analysis(review)
            if score > 0.8:
                await ts.reviews.approve(review.id)
            elif score > 0.5:
                await ts.reviews.request_refinement(
                    review.id,
                    comment="Please add more supporting evidence.",
                )
            else:
                await ts.reviews.reject(review.id, comment="Quality below threshold.")

asyncio.run(process_queue())
```
