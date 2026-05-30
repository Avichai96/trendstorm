"""MongoDB implementation of InviteRepository."""

from __future__ import annotations

from typing import Any, ClassVar

from pymongo import DESCENDING

from trendstorm.domain.invites.models import Invite
from trendstorm.infrastructure.mongo.repositories._base import (
    TenantScopedRepository,
    now_utc,
)
from trendstorm.infrastructure.mongo.schema import Collection
from trendstorm.shared.logging import get_logger

logger = get_logger(__name__)


class MongoInviteRepository(TenantScopedRepository[Invite]):
    collection: ClassVar[Collection] = Collection.INVITES
    model: ClassVar[type[Invite]] = Invite

    async def insert(self, invite: Invite, *, session: object = None) -> None:
        await self._insert(
            self._encode(invite),
            what=f"Invite {invite.email} in {invite.tenant_id}",
            session=session,
        )

    async def get(self, tenant_id: str, invite_id: str) -> Invite | None:
        doc = await self._find_one(
            self._tenant_query(tenant_id, _id=invite_id),
            what=f"Invite {invite_id}",
        )
        return self._decode(doc) if doc else None

    async def get_by_token_hash(self, token_hash: str) -> Invite | None:
        """Public lookup — not tenant-scoped because tenant is unknown before token resolution."""
        from pymongo.errors import PyMongoError

        from trendstorm.infrastructure.mongo.repositories._base import raise_db_error
        try:
            doc = await self._coll.find_one({"token_hash": token_hash})
        except PyMongoError as e:
            raise_db_error(e, operation="invite.get_by_token_hash")
        return self._decode(doc) if doc else None

    async def get_pending_for_email(self, tenant_id: str, email: str) -> Invite | None:
        doc = await self._find_one(
            self._tenant_query(
                tenant_id, email=email.lower(), accepted_at=None, revoked_at=None
            ),
            what=f"pending Invite {email}",
        )
        return self._decode(doc) if doc else None

    async def list_pending_for_tenant(
        self,
        tenant_id: str,
        *,
        limit: int = 50,
        before_id: str | None = None,
    ) -> list[Invite]:
        query = self._tenant_query(tenant_id, accepted_at=None, revoked_at=None)
        if before_id is not None:
            query["_id"] = {"$lt": before_id}
        docs = await self._find_many(
            query,
            sort=[("_id", DESCENDING)],
            limit=limit,
            what="pending Invite list",
        )
        return [self._decode(d) for d in docs]

    async def accept(self, tenant_id: str, invite_id: str, *, session: Any | None = None) -> Invite | None:
        from pymongo.errors import PyMongoError

        from trendstorm.infrastructure.mongo.repositories._base import raise_db_error
        try:
            query = self._tenant_query(tenant_id, _id=invite_id, accepted_at=None, revoked_at=None)
            update = {"$set": {"accepted_at": now_utc()}}
            if session is not None:
                doc = await self._coll.find_one_and_update(
                    query, update, return_document=True, session=session
                )
            else:
                doc = await self._coll.find_one_and_update(
                    query, update, return_document=True
                )
        except PyMongoError as e:
            raise_db_error(e, operation="invite.accept", invite_id=invite_id)
        return self._decode(doc) if doc else None

    async def revoke(self, tenant_id: str, invite_id: str) -> Invite | None:
        from pymongo.errors import PyMongoError

        from trendstorm.infrastructure.mongo.repositories._base import raise_db_error
        try:
            doc = await self._coll.find_one_and_update(
                self._tenant_query(tenant_id, _id=invite_id, accepted_at=None, revoked_at=None),
                {"$set": {"revoked_at": now_utc()}},
                return_document=True,
            )
        except PyMongoError as e:
            raise_db_error(e, operation="invite.revoke", invite_id=invite_id)
        return self._decode(doc) if doc else None
