"""MembershipRepository Protocol."""

from __future__ import annotations

from typing import Any, Protocol

from trendstorm.domain.memberships.models import Membership, Role


class MembershipRepository(Protocol):
    async def insert(self, membership: Membership, *, session: Any | None = None) -> None: ...

    async def get(self, tenant_id: str, membership_id: str) -> Membership | None: ...

    async def get_for_user(self, tenant_id: str, user_id: str) -> Membership | None:
        """Return the single membership for this user in this org, or None."""
        ...

    async def list_for_tenant(self, tenant_id: str) -> list[Membership]: ...

    async def list_for_user(self, user_id: str) -> list[Membership]:
        """All memberships for this user across all orgs. Not tenant-scoped."""
        ...

    async def list_admins_for_tenant(self, tenant_id: str) -> list[Membership]:
        """Users with OWNER or ADMIN role — for ownership transfer logic."""
        ...

    async def update_roles(
        self, tenant_id: str, membership_id: str, roles: list[Role]
    ) -> Membership | None: ...

    async def update_last_active(self, tenant_id: str, user_id: str) -> None: ...

    async def delete(
        self, tenant_id: str, membership_id: str, *, session: Any | None = None
    ) -> None: ...

    async def delete_all_for_user(
        self, user_id: str, *, session: Any | None = None
    ) -> None:
        """Remove all memberships when a user account is purged."""
        ...
