"""QuotaService — pre-flight quota check before job creation.

Called by JobService before inserting a new job. Returns a QuotaStatus that
the caller converts to HTTP 402 when `allowed=False`.

Design:
- Monthly spend and job count are read from the cost ledger (append-only).
- Plan quotas come from PLAN_QUOTAS constant; they are NOT per-tenant rows in
  Mongo — quotas are policy, not data. Changing a plan's quotas requires a
  deploy, not a migration.
- The check is a best-effort advisory: a race between two simultaneous job
  creates can both pass. The hard backstop is the cost ledger TTL + billing
  alert (ops/runbooks/cost-overrun.md). We accept this trade-off to avoid
  a distributed lock on job creation.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from opentelemetry import trace

from trendstorm.domain.billing.models import PLAN_QUOTAS, QuotaStatus, TenantQuotas
from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    from trendstorm.domain.auth.repository import TenantRepository
    from trendstorm.domain.billing.repository import CostLedgerRepository


logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)


class QuotaService:
    """Check per-tenant spending quotas before job execution."""

    def __init__(
        self,
        *,
        ledger: CostLedgerRepository,
        tenants: TenantRepository,
    ) -> None:
        self._ledger = ledger
        self._tenants = tenants

    async def check(self, tenant_id: str) -> QuotaStatus:
        """Return a QuotaStatus for the tenant's current month.

        The tenant's plan determines which TenantQuotas apply.
        """
        with tracer.start_as_current_span("quota.check"):
            now = datetime.now(UTC)
            year, month = now.year, now.month

            # Load tenant plan; default to "free" if not found (paranoia — tenant
            # should always exist by this point in the request lifecycle).
            tenant = await self._tenants.get(tenant_id)
            plan = tenant.plan if tenant else "free"
            quotas: TenantQuotas = PLAN_QUOTAS.get(plan, PLAN_QUOTAS["free"])

            # Parallel reads from the ledger.
            import asyncio
            spend_micro, job_count = await asyncio.gather(
                self._ledger.monthly_spend_usd_micro(tenant_id, year, month),
                self._ledger.jobs_this_month(tenant_id, year, month),
            )

            spend_allowed = spend_micro < quotas.monthly_spend_usd_micro
            jobs_allowed = job_count < quotas.max_jobs_per_month

            if not spend_allowed:
                reason = (
                    f"Monthly spend limit reached "
                    f"(${spend_micro / 1_000_000:.2f} of "
                    f"${quotas.monthly_spend_usd_micro / 1_000_000:.2f})."
                )
                logger.warning(
                    "quota.spend_exceeded",
                    tenant_id=tenant_id,
                    spend_usd_micro=spend_micro,
                    limit_usd_micro=quotas.monthly_spend_usd_micro,
                )
            elif not jobs_allowed:
                reason = (
                    f"Monthly job limit reached "
                    f"({job_count} of {quotas.max_jobs_per_month})."
                )
                logger.warning(
                    "quota.jobs_exceeded",
                    tenant_id=tenant_id,
                    job_count=job_count,
                    limit=quotas.max_jobs_per_month,
                )
            else:
                reason = None

            return QuotaStatus(
                allowed=spend_allowed and jobs_allowed,
                monthly_spend_usd_micro=spend_micro,
                quota_spend_usd_micro=quotas.monthly_spend_usd_micro,
                jobs_this_month=job_count,
                quota_jobs=quotas.max_jobs_per_month,
                reason=reason,
            )
