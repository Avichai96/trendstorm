"""MongoDB implementation of MembershipRepository."""

from __future__ import annotations

from typing import Any, ClassVar

from pymongo import DESCENDING

from trendstorm.domain.memberships.models import Membership, Role
from trendstorm.infrastructure.mongo.repositories._base import (
    TenantScopedRepository,
    now_utc,
)
from trendstorm.infrastructure.mongo.schema import Collection
from trendstorm.shared.logging import get_logger

logger = get_logger(__name__)


class MongoMembershipRepository(TenantScopedRepository[Membership]):
    collection: ClassVar[Collection] = Collection.MEMBERSHIPS
    model: ClassVar[type[Membership]] = Membership

    async def insert(self, membership: Membership, *, session: Any | None = None) -> None:
        await self._insert(
            self._encode(membership),
            what=f"Membership {membership.user_id} in {membership.tenant_id}",
            session=session,
        )

    async def get(self, tenant_id: str, membership_id: str) -> Membership | None:
        doc = await self._find_one(
            self._tenant_query(tenant_id, _id=membership_id),
            what=f"Membership {membership_id}",
        )
        return self._decode(doc) if doc else None

    async def get_for_user(self, tenant_id: str, user_id: str) -> Membership | None:
        doc = await self._find_one(
            self._tenant_query(tenant_id, user_id=user_id),
            what=f"Membership user={user_id} in {tenant_id}",
        )
        return self._decode(doc) if doc else None

    async def list_for_tenant(self, tenant_id: str) -> list[Membership]:
        docs = await self._find_many(
            self._tenant_query(tenant_id),
            sort=[("_id", DESCENDING)],
            what="Membership list",
        )
        return [self._decode(d) for d in docs]

    async def list_for_user(self, user_id: str) -> list[Membership]:
        """Cross-tenant: all memberships for a user. Not filtered by tenant_id."""
        from pymongo.errors import PyMongoError

        from trendstorm.infrastructure.mongo.repositories._base import raise_db_error
        try:
            docs = await self._coll.find({"user_id": user_id}).to_list(length=None)
        except PyMongoError as e:
            raise_db_error(e, operation="membership.list_for_user", user_id=user_id)
        return [self._decode(d) for d in docs]

    async def list_admins_for_tenant(self, tenant_id: str) -> list[Membership]:
        """Members with OWNER or ADMIN role — for ownership transfer logic."""
        docs = await self._find_many(
            self._tenant_query(tenant_id, roles={"$in": [Role.OWNER.value, Role.ADMIN.value]}),
            what="admin Membership list",
        )
        return [self._decode(d) for d in docs]

    async def update_roles(
        self, tenant_id: str, membership_id: str, roles: list[Role]
    ) -> Membership | None:
        from pymongo.errors import PyMongoError

        from trendstorm.infrastructure.mongo.repositories._base import raise_db_error
        try:
            doc = await self._coll.find_one_and_update(
                self._tenant_query(tenant_id, _id=membership_id),
                {"$set": {"roles": [r.value for r in roles]}},
                return_document=True,
            )
        except PyMongoError as e:
            raise_db_error(e, operation="membership.update_roles", membership_id=membership_id)
        return self._decode(doc) if doc else None

    async def update_last_active(self, tenant_id: str, user_id: str) -> None:
        from pymongo.errors import PyMongoError

        from trendstorm.infrastructure.mongo.repositories._base import raise_db_error
        try:
            await self._coll.update_one(
                self._tenant_query(tenant_id, user_id=user_id),
                {"$set": {"last_active_at": now_utc()}},
            )
        except PyMongoError as e:
            raise_db_error(e, operation="membership.update_last_active")

    async def delete(
        self, tenant_id: str, membership_id: str, *, session: Any | None = None
    ) -> None:
        from pymongo.errors import PyMongoError

        from trendstorm.infrastructure.mongo.repositories._base import raise_db_error
        try:
            if session is not None:
                await self._coll.delete_one(
                    self._tenant_query(tenant_id, _id=membership_id),
                    session=session,
                )
            else:
                await self._coll.delete_one(
                    self._tenant_query(tenant_id, _id=membership_id)
                )
        except PyMongoError as e:
            raise_db_error(e, operation="membership.delete", membership_id=membership_id)

    async def delete_all_for_user(self, user_id: str, *, session: Any | None = None) -> None:
        """Cross-tenant bulk delete — used by execute_purge()."""
        from pymongo.errors import PyMongoError

        from trendstorm.infrastructure.mongo.repositories._base import raise_db_error
        try:
            if session is not None:
                await self._coll.delete_many({"user_id": user_id}, session=session)
            else:
                await self._coll.delete_many({"user_id": user_id})
        except PyMongoError as e:
            raise_db_error(e, operation="membership.delete_all_for_user", user_id=user_id)
