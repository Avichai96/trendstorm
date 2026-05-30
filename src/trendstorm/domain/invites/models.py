"""Invite domain model.

An Invite is a pending invitation for a specific email to join an organization.
The invite token is generated as 32 random bytes (URL-safe base64), and only
the SHA-256 hash is stored — same pattern as API keys.

Uniqueness: exactly one pending invite per (tenant_id, email) is enforced
by a partial unique index (accepted_at IS NULL AND revoked_at IS NULL).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from trendstorm.domain.memberships.models import Role
from trendstorm.shared.ids import new_id

_DEFAULT_EXPIRY_DAYS = 7


class InviteStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REVOKED = "revoked"
    EXPIRED = "expired"


class Invite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    tenant_id: str
    email: str  # lowercase; target recipient
    token_hash: str  # SHA-256 of the 32-byte plaintext token
    roles: list[Role]
    invited_by_user_id: str
    expires_at: datetime
    accepted_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_pending(self) -> bool:
        return self.accepted_at is None and self.revoked_at is None

    @property
    def is_expired(self) -> bool:
        return self.is_pending and datetime.now(UTC) > self.expires_at

    @property
    def status(self) -> InviteStatus:
        if self.accepted_at is not None:
            return InviteStatus.ACCEPTED
        if self.revoked_at is not None:
            return InviteStatus.REVOKED
        if datetime.now(UTC) > self.expires_at:
            return InviteStatus.EXPIRED
        return InviteStatus.PENDING
