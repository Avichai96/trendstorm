"""Billing domain models.

CostLedgerEntry — one row per LLM call, recording token usage and estimated
cost for a specific tenant/job/stage combination. The ledger is the source
of truth for per-tenant cost aggregation; Prometheus metrics are the
operational signal (cardinality-safe gauges and counters) while the ledger
supports per-job breakdowns and billing reconciliation.

TenantQuotas — per-plan monthly limits. These are policy (not measured usage).
QuotaStatus — result of a pre-flight check: whether the tenant's monthly spend
is within their plan's limits.

Cost units:
  All monetary amounts are in USD x 10^-6 (micro-dollars) stored as integers
  to avoid floating-point rounding. Presentation layer divides by 1_000_000.
  Token prices follow the constants in `services/billing/prices.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from trendstorm.shared.ids import new_id

ModelProvider = Literal["anthropic", "openai", "gemini", "ollama", "cohere"]
LedgerStage = Literal[
    "analysis_analyst",
    "analysis_validator",
    "query_expansion",
    "rerank",
    "embedding",
]


class CostLedgerEntry(BaseModel):
    """Immutable record of a single LLM billing event.

    Written by `record_llm_cost()` in `shared/metrics/cost.py` after every
    structured LLM call. Never updated in place — cost records are append-only.
    TTL: 90 days (same as jobs, for billing reconciliation window).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    tenant_id: str
    job_id: str
    stage: LedgerStage
    provider: ModelProvider
    model_id: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int = 0
    # Micro-dollars (USD x 10^-6); computed by billing service from token counts.
    cost_usd_micro: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TenantQuotas(BaseModel):
    """Monthly spending quota per plan tier.

    Amounts in micro-dollars. Defaults match the free tier.
    Override in `BillingSettings` per plan.
    """

    model_config = ConfigDict(extra="forbid")

    monthly_spend_usd_micro: int = 10_000_000  # $10 default (free tier)
    max_jobs_per_month: int = 50
    max_tokens_per_job: int = 500_000


class QuotaStatus(BaseModel):
    """Result of a pre-flight quota check before starting a job.

    `allowed` is False when ANY limit is breached. Callers (JobService,
    QuotaMiddleware) must refuse the request with HTTP 402 when denied.
    """

    model_config = ConfigDict(extra="forbid")

    allowed: bool
    monthly_spend_usd_micro: int  # accumulated this month
    quota_spend_usd_micro: int  # tenant's monthly limit
    jobs_this_month: int
    quota_jobs: int
    reason: str | None = None  # human-readable when denied


# Plan → default quotas mapping (read by QuotaService, not stored in Mongo)
PLAN_QUOTAS: dict[str, TenantQuotas] = {
    "free": TenantQuotas(
        monthly_spend_usd_micro=10_000_000,  # $10
        max_jobs_per_month=50,
        max_tokens_per_job=500_000,
    ),
    "pro": TenantQuotas(
        monthly_spend_usd_micro=100_000_000,  # $100
        max_jobs_per_month=500,
        max_tokens_per_job=2_000_000,
    ),
    "enterprise": TenantQuotas(
        monthly_spend_usd_micro=1_000_000_000,  # $1000
        max_jobs_per_month=10_000,
        max_tokens_per_job=10_000_000,
    ),
}
