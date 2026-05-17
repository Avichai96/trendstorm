"""MongoDB implementation of CostLedgerRepository."""
from __future__ import annotations

from datetime import UTC
from typing import Any

from trendstorm.domain.billing.models import CostLedgerEntry
from trendstorm.infrastructure.mongo.repositories._base import TenantScopedRepository
from trendstorm.infrastructure.mongo.schema import Collection


class MongoCostLedgerRepository(TenantScopedRepository[CostLedgerEntry]):
    collection = Collection.COST_LEDGER
    model = CostLedgerEntry

    async def insert(self, entry: CostLedgerEntry) -> None:
        doc = self._encode(entry)
        await self._insert(doc, what="cost_ledger_entry")

    async def monthly_spend_usd_micro(
        self,
        tenant_id: str,
        year: int,
        month: int,
    ) -> int:
        """Sum cost_usd_micro for the tenant in the given calendar month.

        Aggregation pipeline:
          $match → $group($sum) — hits the cost_ledger__tenant_created index.
        """
        from datetime import datetime

        start = datetime(year, month, 1, tzinfo=UTC)
        if month == 12:
            end = datetime(year + 1, 1, 1, tzinfo=UTC)
        else:
            end = datetime(year, month + 1, 1, tzinfo=UTC)

        pipeline: list[dict[str, Any]] = [
            {
                "$match": {
                    **self._tenant_query(tenant_id),
                    "created_at": {"$gte": start, "$lt": end},
                }
            },
            {
                "$group": {
                    "_id": None,
                    "total": {"$sum": "$cost_usd_micro"},
                }
            },
        ]
        coll = self._coll
        cursor = coll.aggregate(pipeline)
        async for doc in cursor:
            return int(doc["total"])
        return 0

    async def jobs_this_month(
        self,
        tenant_id: str,
        year: int,
        month: int,
    ) -> int:
        """Count distinct job_ids billed for the tenant in the given month."""
        from datetime import datetime

        start = datetime(year, month, 1, tzinfo=UTC)
        if month == 12:
            end = datetime(year + 1, 1, 1, tzinfo=UTC)
        else:
            end = datetime(year, month + 1, 1, tzinfo=UTC)

        pipeline: list[dict[str, Any]] = [
            {
                "$match": {
                    **self._tenant_query(tenant_id),
                    "created_at": {"$gte": start, "$lt": end},
                }
            },
            {
                "$group": {"_id": "$job_id"},
            },
            {
                "$count": "n",
            },
        ]
        coll = self._coll
        cursor = coll.aggregate(pipeline)
        async for doc in cursor:
            return int(doc["n"])
        return 0
