"""Membership domain model — user ↔ organization link.

A Membership ties a User to an Organization with one or more Roles. It is
tenant-scoped (tenant_id == organization_id) so it follows the standard
_tenant_query() pattern.

Unique constraint: (tenant_id, user_id) — one membership per user per org.
A user can have memberships in multiple orgs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from trendstorm.shared.ids import new_id


class Role(StrEnum):
    """Roles within an organization.

    OWNER    — full access; transferred on account purge.
    ADMIN    — manage members and org settings.
    MEMBER   — create jobs, manage categories and sources.
    REVIEWER — approve/reject HITL review queue items (maps to existing
               "reviewer" string on ApiKey.roles — backward-compatible).
    VIEWER   — read-only access.
    """

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    REVIEWER = "reviewer"
    VIEWER = "viewer"


class Membership(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    tenant_id: str  # == organization_id
    user_id: str
    roles: list[Role]
    invited_by_user_id: str | None = None
    joined_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_active_at: datetime | None = None

    @property
    def is_owner(self) -> bool:
        return Role.OWNER in self.roles

    @property
    def is_admin_or_above(self) -> bool:
        return bool({Role.OWNER, Role.ADMIN} & set(self.roles))

    def has_role(self, *roles: Role) -> bool:
        return bool(set(roles) & set(self.roles))
