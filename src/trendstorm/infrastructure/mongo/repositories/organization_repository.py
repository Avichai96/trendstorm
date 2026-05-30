"""MongoDB implementation of OrganizationRepository.

Uses Collection.TENANTS (Mongo collection name "tenants") to avoid a data
migration of existing Tenant documents. The Python class is Organization;
the wire name is unchanged.
"""

from __future__ import annotations

from typing import Any

from pymongo.errors import DuplicateKeyError, PyMongoError

from trendstorm.domain.organizations.models import Organization
from trendstorm.infrastructure.mongo.repositories._base import (
    from_mongo_doc,
    raise_db_error,
    raise_on_dup_key,
    to_mongo_doc,
)
from trendstorm.infrastructure.mongo.schema import Collection
from trendstorm.shared.logging import get_logger

logger = get_logger(__name__)


class MongoOrganizationRepository:
    """Concrete OrganizationRepository backed by MongoDB."""

    def __init__(self, mongo: Any) -> None:
        self._mongo = mongo

    @property
    def _coll(self) -> Any:
        return self._mongo.db[Collection.TENANTS.value]

    def _encode(self, org: Organization) -> dict[str, Any]:
        return to_mongo_doc(org.model_dump(mode="json"))

    def _decode(self, doc: dict[str, Any]) -> Organization:
        return Organization.model_validate(from_mongo_doc(doc))

    async def insert(self, org: Organization, *, session: Any | None = None) -> None:
        try:
            doc = self._encode(org)
            if session is not None:
                await self._coll.insert_one(doc, session=session)
            else:
                await self._coll.insert_one(doc)
        except DuplicateKeyError as e:
            raise_on_dup_key(e, what=f"Organization {org.name}")
        except PyMongoError as e:
            raise_db_error(e, operation="organization.insert", name=org.name)

    async def get(self, org_id: str) -> Organization | None:
        try:
            doc = await self._coll.find_one({"_id": org_id})
        except PyMongoError as e:
            raise_db_error(e, operation="organization.get", org_id=org_id)
        return self._decode(doc) if doc else None

    async def get_by_slug(self, slug: str) -> Organization | None:
        try:
            doc = await self._coll.find_one({"slug": slug})
        except PyMongoError as e:
            raise_db_error(e, operation="organization.get_by_slug", slug=slug)
        return self._decode(doc) if doc else None

    async def get_by_name(self, name: str) -> Organization | None:
        try:
            doc = await self._coll.find_one({"name": name})
        except PyMongoError as e:
            raise_db_error(e, operation="organization.get_by_name", name=name)
        return self._decode(doc) if doc else None

    async def update(self, org: Organization) -> None:
        try:
            doc = self._encode(org)
            doc.pop("_id", None)
            await self._coll.update_one({"_id": org.id}, {"$set": doc})
        except PyMongoError as e:
            raise_db_error(e, operation="organization.update", org_id=org.id)

    async def transfer_ownership(self, org_id: str, new_owner_user_id: str) -> Organization | None:
        try:
            doc = await self._coll.find_one_and_update(
                {"_id": org_id},
                {"$set": {"owner_user_id": new_owner_user_id}},
                return_document=True,
            )
        except PyMongoError as e:
            raise_db_error(e, operation="organization.transfer_ownership", org_id=org_id)
        return self._decode(doc) if doc else None

    async def mark_orphaned(self, org_id: str) -> Organization | None:
        """Nullify owner when sole owner is purged and no other admin exists."""
        try:
            doc = await self._coll.find_one_and_update(
                {"_id": org_id},
                {"$set": {"owner_user_id": None}},
                return_document=True,
            )
        except PyMongoError as e:
            raise_db_error(e, operation="organization.mark_orphaned", org_id=org_id)
        return self._decode(doc) if doc else None

    async def list_for_user(self, user_id: str) -> list[Organization]:
        try:
            docs = await self._coll.find({"owner_user_id": user_id}).to_list(length=None)
        except PyMongoError as e:
            raise_db_error(e, operation="organization.list_for_user", user_id=user_id)
        return [self._decode(d) for d in docs]
