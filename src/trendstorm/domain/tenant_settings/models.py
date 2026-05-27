"""Per-tenant operational settings.

TenantSettings is a mutable, upserted document in the `tenant_settings`
collection. Fields default to safe values so existing tenants without a
settings document see no behavior change (hitl_mode=off is the default).

HitlMode semantics:
    off           — No human-in-the-loop review; analysis goes straight to publish.
    always        — Every analysis requires reviewer approval before publication.
    flagged_only  — Only analyses below hitl_validator_threshold, or that
                    exhausted max refinement loops, or that exceeded
                    hitl_cost_threshold_usd are routed to review.
"""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from trendstorm.shared.ids import new_id


class HitlMode(StrEnum):
    OFF = "off"
    ALWAYS = "always"
    FLAGGED_ONLY = "flagged_only"


class TenantSettings(BaseModel):
    """Operational settings for a single tenant. Optional (defaults apply if absent)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    tenant_id: str

    # HITL configuration
    hitl_mode: HitlMode = HitlMode.OFF
    # Score below which an analysis is flagged for review (flagged_only mode).
    hitl_validator_threshold: float = 0.7
    # USD spend above which a job is flagged for review; None disables cost gating.
    hitl_cost_threshold_usd: float | None = None
    # How long (hours) before a pending review auto-times-out (default: 48h).
    hitl_timeout_hours: int = 48

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# Sentinel returned when no settings row exists for a tenant.
DEFAULT_TENANT_SETTINGS = TenantSettings(
    id="__default__",
    tenant_id="__default__",
)
