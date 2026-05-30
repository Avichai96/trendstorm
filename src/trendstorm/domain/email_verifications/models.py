"""EmailVerification domain model.

Tracks pending email verification tokens. The plaintext token is sent to the
user's inbox; only the SHA-256 hash is persisted. A Mongo TTL index on
created_at expires documents after 7 days (the token window).

consumed_at marks single-use consumption; any subsequent attempt with the same
token is rejected even if the TTL hasn't fired yet.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from trendstorm.shared.ids import new_id

_EXPIRY_HOURS = 24


class EmailVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    user_id: str
    email: str  # the email being verified (may differ from current User.email)
    token_hash: str
    expires_at: datetime  # set to created_at + 24h by the service
    consumed_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_valid(self) -> bool:
        return self.consumed_at is None and datetime.now(UTC) <= self.expires_at
