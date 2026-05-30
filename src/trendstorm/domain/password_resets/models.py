"""PasswordReset domain model.

Short-lived (1h) token for the password reset flow. Only the SHA-256 hash
is persisted. consumed_at enforces single-use. The TTL index on created_at
auto-cleans expired unclaimed records after 1 hour.

We manage this token ourselves (not Auth0's built-in reset email) so our
dashboard owns the /auth/reset UI and PostMark sends the branded email.
Consuming the token calls IdentityProvider.set_password() on Auth0.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from trendstorm.shared.ids import new_id

_EXPIRY_MINUTES = 60


class PasswordReset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    user_id: str
    token_hash: str
    expires_at: datetime  # created_at + 60 minutes
    consumed_at: datetime | None = None
    requested_from_ip: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_valid(self) -> bool:
        return self.consumed_at is None and datetime.now(UTC) <= self.expires_at
