"""InviteRepository Protocol."""

from __future__ import annotations

from typing import Any, Protocol

from trendstorm.domain.invites.models import Invite


class InviteRepository(Protocol):
    async def insert(self, invite: Invite, *, session: Any | None = None) -> None: ...

    async def get(self, tenant_id: str, invite_id: str) -> Invite | None: ...

    async def get_by_token_hash(self, token_hash: str) -> Invite | None:
        """Look up invite by token hash.

        Not tenant-scoped because the tenant is not known before the token is resolved.
        """
        ...

    async def get_pending_for_email(self, tenant_id: str, email: str) -> Invite | None:
        """Return the pending invite for this email in this org, or None."""
        ...

    async def list_pending_for_tenant(
        self,
        tenant_id: str,
        *,
        limit: int = 50,
        before_id: str | None = None,
    ) -> list[Invite]: ...

    async def accept(self, tenant_id: str, invite_id: str, *, session: Any | None = None) -> Invite | None:
        """Set accepted_at = now(). Idempotent if already accepted."""
        ...

    async def revoke(self, tenant_id: str, invite_id: str) -> Invite | None:
        """Set revoked_at = now(). Idempotent if already revoked."""
        ...
