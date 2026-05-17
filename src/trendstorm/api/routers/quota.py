"""Quota router — per-tenant spending and job quota status.

GET /v1/quota — return current month's spend + limits for the authenticated tenant.

The quota check on job creation is enforced by JobService (via QuotaService).
This endpoint exposes the current status so clients can show usage meters
without attempting a job create and getting a 402.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from trendstorm.api.deps import MongoDep
from trendstorm.utils.headers_docs import require_tenant

router = APIRouter(
    prefix="/v1/quota",
    tags=["quota"],
    dependencies=[Depends(require_tenant)],
)


class QuotaResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed: bool
    monthly_spend_usd: float
    monthly_limit_usd: float
    jobs_this_month: int
    jobs_limit: int
    reason: str | None = None


@router.get(
    "",
    response_model=QuotaResponse,
    summary="Get current month's quota status",
)
async def get_quota(
    request: Request,
    mongo: MongoDep,
) -> QuotaResponse:
    from trendstorm.infrastructure.mongo.repositories.cost_ledger_repository import (
        MongoCostLedgerRepository,
    )
    from trendstorm.infrastructure.mongo.repositories.tenant_repository import MongoTenantRepository
    from trendstorm.services.billing.quota_service import QuotaService

    ledger_repo = MongoCostLedgerRepository(mongo)
    tenant_repo = MongoTenantRepository(mongo)
    quota_svc = QuotaService(ledger=ledger_repo, tenants=tenant_repo)

    status = await quota_svc.check(request.state.tenant_id)
    return QuotaResponse(
        allowed=status.allowed,
        monthly_spend_usd=status.monthly_spend_usd_micro / 1_000_000,
        monthly_limit_usd=status.quota_spend_usd_micro / 1_000_000,
        jobs_this_month=status.jobs_this_month,
        jobs_limit=status.quota_jobs,
        reason=status.reason,
    )
