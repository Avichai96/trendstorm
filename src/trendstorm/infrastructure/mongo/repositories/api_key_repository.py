"""MongoDB implementation of ApiKeyRepository."""
from __future__ import annotations

from typing import ClassVar

from pymongo.errors import DuplicateKeyError, PyMongoError

from trendstorm.domain.auth.models import ApiKey
from trendstorm.infrastructure.mongo.repositories._base import (
    TenantScopedRepository,
    now_utc,
    raise_db_error,
    raise_on_dup_key,
)
from trendstorm.infrastructure.mongo.schema import Collection
from trendstorm.shared.logging import get_logger

logger = get_logger(__name__)


class MongoApiKeyRepository(TenantScopedRepository[ApiKey]):
    """Concrete ApiKeyRepository backed by MongoDB."""

    collection: ClassVar[Collection] = Collection.API_KEYS
    model: ClassVar[type[ApiKey]] = ApiKey

    async def insert(self, key: ApiKey) -> None:
        try:
            await self._insert(self._encode(key), what=f"ApiKey {key.id}")
        except DuplicateKeyError as e:
            raise_on_dup_key(e, what=f"ApiKey {key.id}")

    async def get_by_hash(self, key_hash: str) -> ApiKey | None:
        """Hot path: called on every authenticated request."""
        try:
            doc = await self._coll.find_one({"key_hash": key_hash})
        except PyMongoError as e:
            raise_db_error(e, operation="api_key.get_by_hash")
        return self._decode(doc) if doc else None

    async def get_by_id(self, tenant_id: str, key_id: str) -> ApiKey | None:
        doc = await self._find_one(
            self._tenant_query(tenant_id, _id=key_id),
            what=f"ApiKey {key_id}",
        )
        return self._decode(doc) if doc else None

    async def list_for_tenant(self, tenant_id: str) -> list[ApiKey]:
        docs = await self._find_many(
            self._tenant_query(tenant_id),
            sort=[("created_at", -1)],
            what="api_keys list",
        )
        return [self._decode(d) for d in docs]

    async def revoke(self, tenant_id: str, key_id: str) -> None:
        try:
            await self._coll.update_one(
                self._tenant_query(tenant_id, _id=key_id),
                {"$set": {"revoked_at": now_utc()}},
            )
        except PyMongoError as e:
            raise_db_error(e, operation="api_key.revoke", key_id=key_id)

    async def update_last_used(self, key_id: str) -> None:
        """Best-effort stamp; no tenant scope needed — id is globally unique."""
        try:
            await self._coll.update_one(
                {"_id": key_id},
                {"$set": {"last_used_at": now_utc()}},
            )
        except PyMongoError as e:
            # Best-effort; callers fire-and-forget.
            logger.warning("api_key.update_last_used_failed", key_id=key_id, error=str(e))
