"""Billing domain repository Protocol."""
from __future__ import annotations

from typing import Protocol

from trendstorm.domain.billing.models import CostLedgerEntry


class CostLedgerRepository(Protocol):
    async def insert(self, entry: CostLedgerEntry) -> None: ...

    async def monthly_spend_usd_micro(
        self,
        tenant_id: str,
        year: int,
        month: int,
    ) -> int:
        """Return the sum of cost_usd_micro for tenant in the given month."""
        ...

    async def jobs_this_month(
        self,
        tenant_id: str,
        year: int,
        month: int,
    ) -> int:
        """Return the number of distinct job_ids billed in the given month."""
        ...
