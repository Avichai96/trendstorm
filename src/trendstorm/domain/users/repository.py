"""UserRepository Protocol — persistence contract for User records."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from trendstorm.domain.users.models import User


class UserRepository(Protocol):
    """Top-level (non-tenant-scoped) repository for User records.

    Users exist outside the tenant scope — they are the actors who belong
    to organizations, not resources owned by an organization. MongoUserRepository
    is the documented exception to the _tenant_query() rule (Rule 3).
    """

    async def insert(self, user: User, *, session: Any | None = None) -> None: ...

    async def get(self, user_id: str) -> User | None: ...

    async def get_by_email(self, email: str) -> User | None:
        """Case-insensitive lookup via collation index."""
        ...

    async def get_by_subject(self, subject: str) -> User | None:
        """Look up by identity_provider_subject (Auth0 sub)."""
        ...

    async def update(self, user: User) -> None: ...

    async def tombstone(self, user_id: str, *, deleted_at: datetime, purge_at: datetime) -> User | None:
        """Soft-delete: set deleted_at and purge_at atomically."""
        ...

    async def cancel_tombstone(self, user_id: str) -> User | None:
        """Clear deleted_at and purge_at (within deletion window)."""
        ...

    async def list_due_for_purge(self, *, limit: int = 50) -> list[User]:
        """Return users whose purge_at <= now(). Called by the purge sweeper."""
        ...

    async def hard_delete(self, user_id: str) -> None:
        """Permanently remove the user record. Called only by execute_purge()."""
        ...

    async def set_email_verified(self, user_id: str) -> User | None: ...
