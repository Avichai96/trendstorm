"""MongoDB implementation of TenantRepository."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pymongo.errors import DuplicateKeyError, PyMongoError

from trendstorm.domain.auth.models import Tenant
from trendstorm.infrastructure.mongo.repositories._base import (
    raise_db_error,
    raise_on_dup_key,
)
from trendstorm.infrastructure.mongo.schema import Collection
from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    from trendstorm.infrastructure.mongo.client import MongoClient

logger = get_logger(__name__)


class MongoTenantRepository:
    """Concrete TenantRepository backed by MongoDB.

    Tenants are NOT tenant-scoped (they ARE the root). Does not extend
    TenantScopedRepository — uses the motor collection directly.
    """

    def __init__(self, mongo: MongoClient) -> None:
        self._mongo = mongo

    @property
    def _coll(self) -> Any:
        return self._mongo.db[Collection.TENANTS.value]

    def _encode(self, tenant: Tenant) -> dict[str, Any]:
        doc = tenant.model_dump(mode="json")
        doc["_id"] = doc.pop("id")
        return doc

    def _decode(self, doc: dict[str, Any]) -> Tenant:
        out = dict(doc)
        out["id"] = out.pop("_id")
        return Tenant.model_validate(out)

    async def insert(self, tenant: Tenant) -> None:
        try:
            await self._coll.insert_one(self._encode(tenant))
        except DuplicateKeyError as e:
            raise_on_dup_key(e, what=f"Tenant {tenant.name}")
        except PyMongoError as e:
            raise_db_error(e, operation="tenant.insert", name=tenant.name)

    async def get(self, tenant_id: str) -> Tenant | None:
        try:
            doc = await self._coll.find_one({"_id": tenant_id})
        except PyMongoError as e:
            raise_db_error(e, operation="tenant.get", tenant_id=tenant_id)
        return self._decode(doc) if doc else None

    async def get_by_name(self, name: str) -> Tenant | None:
        try:
            doc = await self._coll.find_one({"name": name})
        except PyMongoError as e:
            raise_db_error(e, operation="tenant.get_by_name", name=name)
        return self._decode(doc) if doc else None
