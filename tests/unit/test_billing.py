"""Unit tests for billing domain: price computation, quota service, cost ledger entry."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from trendstorm.domain.billing.models import (
    PLAN_QUOTAS,
    CostLedgerEntry,
)
from trendstorm.services.billing.prices import compute_cost_usd_micro
from trendstorm.services.billing.quota_service import QuotaService

# ---------------------------------------------------------------------------
# Price table
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestPriceComputation:
    def test_zero_tokens_is_zero_cost(self) -> None:
        cost = compute_cost_usd_micro(
            provider="anthropic",
            model_id="claude-sonnet-4-6",
            input_tokens=0,
            output_tokens=0,
        )
        assert cost == 0

    def test_anthropic_sonnet_input_cost(self) -> None:
        # claude-sonnet-4-6: $3/1M input = 0.3 cents/1k = 3000 micro-dollars/1k
        cost = compute_cost_usd_micro(
            provider="anthropic",
            model_id="claude-sonnet-4-6",
            input_tokens=1000,
            output_tokens=0,
        )
        assert cost == 3000  # $0.003 → 3000 micro-dollars

    def test_anthropic_sonnet_output_cost(self) -> None:
        # claude-sonnet-4-6: $15/1M output = 15000 micro-dollars/1k
        cost = compute_cost_usd_micro(
            provider="anthropic",
            model_id="claude-sonnet-4-6",
            input_tokens=0,
            output_tokens=1000,
        )
        assert cost == 15000

    def test_ollama_is_free(self) -> None:
        cost = compute_cost_usd_micro(
            provider="ollama",
            model_id="llama3.2:3b",
            input_tokens=10_000,
            output_tokens=5_000,
        )
        assert cost == 0

    def test_cached_tokens_billed_at_10_percent(self) -> None:
        # Non-cached 1000 input tokens at claude-sonnet = 3000 micro
        # All 1000 tokens cached → 10% of full cost = 300 micro
        full = compute_cost_usd_micro(
            provider="anthropic",
            model_id="claude-sonnet-4-6",
            input_tokens=1000,
            output_tokens=0,
            cached_tokens=0,
        )
        cached = compute_cost_usd_micro(
            provider="anthropic",
            model_id="claude-sonnet-4-6",
            input_tokens=1000,
            output_tokens=0,
            cached_tokens=1000,
        )
        # All tokens cached → cached cost only (10% of input rate)
        assert cached < full
        assert cached == full // 10

    def test_unknown_model_returns_zero(self) -> None:
        cost = compute_cost_usd_micro(
            provider="anthropic",
            model_id="totally-fake-model-xyz",
            input_tokens=10_000,
            output_tokens=2_000,
        )
        assert cost == 0


# ---------------------------------------------------------------------------
# QuotaService
# ---------------------------------------------------------------------------

def _make_quota_service(
    *,
    spend_micro: int = 0,
    jobs: int = 0,
    plan: str = "free",
) -> QuotaService:
    ledger = MagicMock()
    ledger.monthly_spend_usd_micro = AsyncMock(return_value=spend_micro)
    ledger.jobs_this_month = AsyncMock(return_value=jobs)

    tenant = MagicMock()
    tenant.plan = plan
    tenant_repo = MagicMock()
    tenant_repo.get = AsyncMock(return_value=tenant)

    return QuotaService(ledger=ledger, tenants=tenant_repo)


@pytest.mark.unit
class TestQuotaService:
    @pytest.mark.asyncio
    async def test_within_limits_returns_allowed(self) -> None:
        svc = _make_quota_service(spend_micro=100, jobs=1)
        status = await svc.check("01TENANT000000000000000001")
        assert status.allowed
        assert status.reason is None

    @pytest.mark.asyncio
    async def test_spend_exceeded_returns_denied(self) -> None:
        quotas = PLAN_QUOTAS["free"]
        svc = _make_quota_service(spend_micro=quotas.monthly_spend_usd_micro + 1, jobs=0)
        status = await svc.check("01TENANT000000000000000001")
        assert not status.allowed
        assert status.reason is not None
        assert "spend" in status.reason.lower()

    @pytest.mark.asyncio
    async def test_jobs_exceeded_returns_denied(self) -> None:
        quotas = PLAN_QUOTAS["free"]
        svc = _make_quota_service(spend_micro=0, jobs=quotas.max_jobs_per_month)
        status = await svc.check("01TENANT000000000000000001")
        assert not status.allowed
        assert "job" in (status.reason or "").lower()

    @pytest.mark.asyncio
    async def test_pro_plan_has_higher_limits(self) -> None:
        # Pro plan: $100 limit
        pro_limit = PLAN_QUOTAS["pro"].monthly_spend_usd_micro
        # Spend above free but below pro
        svc = _make_quota_service(
            spend_micro=pro_limit - 1,
            jobs=0,
            plan="pro",
        )
        status = await svc.check("01TENANT000000000000000001")
        assert status.allowed
        assert status.quota_spend_usd_micro == pro_limit

    @pytest.mark.asyncio
    async def test_unknown_tenant_defaults_to_free(self) -> None:
        ledger = MagicMock()
        ledger.monthly_spend_usd_micro = AsyncMock(return_value=0)
        ledger.jobs_this_month = AsyncMock(return_value=0)
        tenant_repo = MagicMock()
        tenant_repo.get = AsyncMock(return_value=None)

        svc = QuotaService(ledger=ledger, tenants=tenant_repo)
        status = await svc.check("01TENANT000000000000000001")
        # Defaults to free tier limits
        assert status.quota_spend_usd_micro == PLAN_QUOTAS["free"].monthly_spend_usd_micro


# ---------------------------------------------------------------------------
# CostLedgerEntry model
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCostLedgerEntry:
    def test_defaults_are_sane(self) -> None:
        entry = CostLedgerEntry(
            tenant_id="01TENANT000000000000000001",
            job_id="01JOB0000000000000000001",
            stage="analysis_analyst",
            provider="anthropic",
            model_id="claude-sonnet-4-6",
            input_tokens=100,
            output_tokens=50,
        )
        assert entry.cached_tokens == 0
        assert entry.cost_usd_micro == 0
        assert entry.created_at.tzinfo is not None   # timezone-aware

    def test_id_is_ulid_length(self) -> None:
        entry = CostLedgerEntry(
            tenant_id="t",
            job_id="j",
            stage="embedding",
            provider="gemini",
            model_id="text-embedding-004",
            input_tokens=1000,
            output_tokens=0,
        )
        assert len(entry.id) == 26
