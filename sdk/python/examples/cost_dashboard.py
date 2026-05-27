"""Cost dashboard — print current month's quota and spending.

Usage:
    export TRENDSTORM_API_KEY=ts_live_...
    export TRENDSTORM_BASE_URL=http://localhost:8080
    python examples/cost_dashboard.py
"""
from __future__ import annotations

import asyncio
import os

from trendstorm_sdk import TrendStormClient


async def main() -> None:
    api_key = os.environ["TRENDSTORM_API_KEY"]
    base_url = os.environ.get("TRENDSTORM_BASE_URL", "https://api.trendstorm.io")

    async with TrendStormClient(api_key=api_key, base_url=base_url) as ts:
        quota = await ts.quota.current_month()

        status_icon = "✅" if quota.allowed else "🚫"
        spend_pct = 100 * quota.monthly_spend_usd / max(quota.monthly_limit_usd, 0.01)
        jobs_pct = 100 * quota.jobs_this_month / max(quota.jobs_limit, 1)

        print(f"\n{'─'*50}")
        print(f"  {status_icon} Quota status: {'OK' if quota.allowed else 'EXCEEDED'}")
        if quota.reason:
            print(f"  Reason: {quota.reason}")
        print(f"{'─'*50}")
        print(f"  Spend:  ${quota.monthly_spend_usd:.4f} / ${quota.monthly_limit_usd:.2f} USD  ({spend_pct:.1f}%)")
        print(f"  Jobs:   {quota.jobs_this_month} / {quota.jobs_limit}  ({jobs_pct:.1f}%)")

        # Simple bar charts
        bar_len = 30
        spend_bar = "█" * int(bar_len * spend_pct / 100) + "░" * (bar_len - int(bar_len * spend_pct / 100))
        jobs_bar = "█" * int(bar_len * jobs_pct / 100) + "░" * (bar_len - int(bar_len * jobs_pct / 100))
        print(f"\n  Spend  [{spend_bar}] {spend_pct:.0f}%")
        print(f"  Jobs   [{jobs_bar}] {jobs_pct:.0f}%")
        print(f"{'─'*50}\n")


if __name__ == "__main__":
    asyncio.run(main())
