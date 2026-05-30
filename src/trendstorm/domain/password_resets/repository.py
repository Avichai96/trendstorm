"""PasswordResetRepository Protocol."""

from __future__ import annotations

from typing import Protocol

from trendstorm.domain.password_resets.models import PasswordReset


class PasswordResetRepository(Protocol):
    async def insert(self, reset: PasswordReset) -> None: ...

    async def get_by_token_hash(self, token_hash: str) -> PasswordReset | None: ...

    async def consume(self, reset_id: str) -> PasswordReset | None:
        """Set consumed_at = now(). Returns None if not found or already consumed."""
        ...

    async def delete_pending_for_user(self, user_id: str) -> None:
        """Invalidate outstanding resets for this user.

        Called when a password is successfully changed or when all sessions are revoked.
        """
        ...
