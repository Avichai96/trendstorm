"""Quota resource — per-tenant spend and job quota status."""
from __future__ import annotations

from trendstorm_shared.models import QuotaResponse

from ._base import AsyncAPIResource


class QuotaResource(AsyncAPIResource):
    """Query current month's quota and spending.

    Examples::

        quota = await ts.quota.current_month()
        if not quota.allowed:
            print(f"Quota exceeded: {quota.monthly_spend_usd:.2f} / {quota.monthly_limit_usd:.2f} USD")
    """

    async def current_month(self) -> QuotaResponse:
        """Return current month's spend and quota limits for the tenant."""
        data = await self._get("/v1/quota")
        return QuotaResponse.model_validate(data)

    async def history(self) -> list[QuotaResponse]:
        """Return monthly quota history.

        Not yet implemented server-side — returns a list containing only the
        current month. Will be expanded in a future API version.
        """
        current = await self.current_month()
        return [current]
