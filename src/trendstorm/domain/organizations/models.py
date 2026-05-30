"""Organization domain model.

An Organization IS the tenant. Its `id` field IS the `tenant_id` that flows
through every tenant-scoped collection in the codebase. The Mongo collection
name stays "tenants" to avoid a data migration; the Python class is renamed
Organization to reflect Phase 16 semantics.

The existing Tenant model in domain/auth/models.py is kept for backward
compatibility during the transition; new code should use Organization.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from trendstorm.shared.ids import new_id

TenantPlan = StrEnum("TenantPlan", ["free", "pro", "enterprise"])


class SignupMode(StrEnum):
    """Controls who can sign up for the platform.

    invite_only — default for production. New accounts require an invite
                  token OR an email that matches an allowlist domain.
    open        — anyone can create an account (useful for public beta).
    closed      — no new signups (maintenance, sunset, etc.).
    """

    INVITE_ONLY = "invite_only"
    OPEN = "open"
    CLOSED = "closed"


class Organization(BaseModel):
    """A tenant / customer organization.

    id == tenant_id throughout the codebase. All tenant-scoped collections
    reference this id as their tenant_id field.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    name: str
    # URL-safe slug: lowercase alphanumeric + hyphens. Unique across all orgs.
    slug: str
    owner_user_id: str
    billing_email: str
    plan: str = "free"  # TenantPlan values
    # Per-org override of the global SIGNUP_MODE setting. None = use global.
    signup_mode_override: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    deleted_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.deleted_at is None

    @property
    def tenant_id(self) -> str:
        """Alias so existing code that references tenant_id still works."""
        return self.id
