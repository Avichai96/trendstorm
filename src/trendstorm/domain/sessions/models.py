"""RefreshSession domain model.

Tracks long-lived refresh tokens. Access tokens (JWTs) are short-lived (15 min)
and stateless; refresh tokens are long-lived (30 days) and stored server-side
so they can be revoked immediately.

Storage:
    Redis   — primary lookup: key="rt:{token_hash}", TTL=expires_at. Fast revocation.
    Mongo   — audit copy: security UI, "sessions" list, post-mortem analysis.

Rotation: on each /auth/refresh call the old refresh token is deleted from Redis
and a new one is issued (refresh token rotation). The Mongo record's last_used_at
is updated and a new record is inserted for the new token.

tenant_id: the active organization context for this session. Switching orgs
(POST /v1/organizations/switch) issues a new access JWT with the new tenant_id
but can reuse the existing refresh session (just updates the tenant_id claim).
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from trendstorm.shared.ids import new_id


class RefreshSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    user_id: str
    tenant_id: str  # active org context at time of issue
    refresh_token_hash: str  # SHA-256 of the plaintext refresh token
    user_agent: str | None = None
    ip_address: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_used_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime
    revoked_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None and datetime.now(UTC) <= self.expires_at
