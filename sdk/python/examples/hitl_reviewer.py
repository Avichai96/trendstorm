"""HITL reviewer — poll pending reviews and resolve them interactively.

This example shows the review queue workflow from a reviewer's perspective.
The API key must have the ``reviewer`` role.

Usage:
    export TRENDSTORM_API_KEY=ts_live_...  # must have reviewer role
    export TRENDSTORM_BASE_URL=http://localhost:8080
    python examples/hitl_reviewer.py
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

from trendstorm_sdk import TrendStormClient
from trendstorm_shared.models import ReviewResponse
from trendstorm_shared.types import ReviewStatus


def _age_hours(review: ReviewResponse) -> float:
    """Hours since review was created."""
    now = datetime.now(timezone.utc)
    return (now - review.created_at).total_seconds() / 3600


def _sla_pct(review: ReviewResponse) -> float:
    """% of SLA elapsed (0-100)."""
    elapsed = (datetime.now(timezone.utc) - review.created_at).total_seconds()
    return min(100.0, 100.0 * elapsed / review.sla_seconds)


async def interactive_review(ts: TrendStormClient) -> None:
    pending = await ts.reviews.list_pending(limit=10)
    if not pending:
        print("No pending reviews. Great work!")
        return

    print(f"\nFound {len(pending)} pending review(s):\n")
    for i, review in enumerate(pending, 1):
        pct = _sla_pct(review)
        urgency = "🔴 URGENT" if pct > 80 else ("🟡" if pct > 50 else "🟢")
        print(f"  {i}. {review.id[:12]}…  job={review.job_id[:12]}…  {urgency} {pct:.0f}% SLA  ({_age_hours(review):.1f}h old)")

    raw = input("\nEnter review number to action (or q to quit): ").strip()
    if raw.lower() == "q":
        return

    idx = int(raw) - 1
    if not (0 <= idx < len(pending)):
        print("Invalid selection.")
        return

    review = pending[idx]
    print(f"\nReview {review.id}")
    print(f"  Job:     {review.job_id}")
    print(f"  Stage:   {review.stage_under_review}")
    print(f"  Timeout: {review.timeout_at.isoformat()}")

    action = input("\n(a)pprove / (r)eject / (f)eedback / (q)uit: ").strip().lower()
    if action == "a":
        resolved = await ts.reviews.approve(review.id)
        print(f"Approved → status={resolved.status}")
    elif action == "r":
        comment = input("Rejection comment (optional): ").strip() or None
        resolved = await ts.reviews.reject(review.id, comment=comment)
        print(f"Rejected → status={resolved.status}")
    elif action == "f":
        comment = input("Refinement notes (required): ").strip()
        if not comment:
            print("Comment required for refinement.")
            return
        resolved = await ts.reviews.request_refinement(review.id, comment=comment)
        print(f"Refinement requested → status={resolved.status}")
    else:
        print("No action taken.")


async def main() -> None:
    api_key = os.environ["TRENDSTORM_API_KEY"]
    base_url = os.environ.get("TRENDSTORM_BASE_URL", "https://api.trendstorm.io")

    async with TrendStormClient(api_key=api_key, base_url=base_url) as ts:
        await interactive_review(ts)


if __name__ == "__main__":
    asyncio.run(main())
