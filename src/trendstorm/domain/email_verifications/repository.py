"""EmailVerificationRepository Protocol."""

from __future__ import annotations

from typing import Protocol

from trendstorm.domain.email_verifications.models import EmailVerification


class EmailVerificationRepository(Protocol):
    async def insert(self, verification: EmailVerification) -> None: ...

    async def get_by_token_hash(self, token_hash: str) -> EmailVerification | None: ...

    async def consume(self, verification_id: str) -> EmailVerification | None:
        """Set consumed_at = now(). Returns None if not found or already consumed."""
        ...

    async def delete_pending_for_user(self, user_id: str) -> None:
        """Remove unconsumed verifications for this user (before issuing a new one)."""
        ...
