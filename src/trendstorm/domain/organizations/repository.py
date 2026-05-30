"""OrganizationRepository Protocol."""

from __future__ import annotations

from typing import Any, Protocol

from trendstorm.domain.organizations.models import Organization


class OrganizationRepository(Protocol):
    """Persistence contract for Organization records.

    Organizations use Collection.TENANTS (same Mongo collection as the legacy
    Tenant model) to avoid a data migration. The repository is not
    tenant-scoped because organizations ARE the root tenant entity.
    """

    async def insert(self, org: Organization, *, session: Any | None = None) -> None: ...

    async def get(self, org_id: str) -> Organization | None: ...

    async def get_by_slug(self, slug: str) -> Organization | None: ...

    async def get_by_name(self, name: str) -> Organization | None: ...

    async def update(self, org: Organization) -> None: ...

    async def transfer_ownership(self, org_id: str, new_owner_user_id: str) -> Organization | None: ...

    async def mark_orphaned(self, org_id: str) -> Organization | None:
        """Mark org as having no owner.

        Set when the sole owner account is purged and no other admin exists.
        NOT a delete — org data is preserved.
        """
        ...

    async def list_for_user(self, user_id: str) -> list[Organization]:
        """Return all orgs where the given user is the owner_user_id.

        For full membership listing, use MembershipRepository.list_for_user.
        """
        ...
