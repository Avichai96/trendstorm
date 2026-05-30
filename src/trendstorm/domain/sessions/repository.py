"""RefreshSessionRepository Protocol."""

from __future__ import annotations

from typing import Protocol

from trendstorm.domain.sessions.models import RefreshSession


class RefreshSessionRepository(Protocol):
    async def insert(self, session: RefreshSession) -> None: ...

    async def get_by_token_hash(self, token_hash: str) -> RefreshSession | None: ...

    async def get(self, session_id: str) -> RefreshSession | None: ...

    async def list_active_for_user(self, user_id: str) -> list[RefreshSession]:
        """Return non-revoked sessions ordered by last_used_at desc (security UI)."""
        ...

    async def update_last_used(self, session_id: str) -> None: ...

    async def revoke(self, session_id: str) -> None:
        """Stamp revoked_at = now(). Redis key is deleted by session_service."""
        ...

    async def revoke_all_for_user(self, user_id: str) -> None:
        """Bulk revoke — used after password reset and account deletion."""
        ...
