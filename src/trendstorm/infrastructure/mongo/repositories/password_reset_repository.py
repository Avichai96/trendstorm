"""MongoDB implementation of PasswordResetRepository."""

from __future__ import annotations

from typing import Any

from pymongo.errors import DuplicateKeyError, PyMongoError

from trendstorm.domain.password_resets.models import PasswordReset
from trendstorm.infrastructure.mongo.repositories._base import (
    from_mongo_doc,
    now_utc,
    raise_db_error,
    raise_on_dup_key,
    to_mongo_doc,
)
from trendstorm.infrastructure.mongo.schema import Collection
from trendstorm.shared.logging import get_logger

logger = get_logger(__name__)


class MongoPasswordResetRepository:
    """Not tenant-scoped — password resets are user-level."""

    def __init__(self, mongo: Any) -> None:
        self._mongo = mongo

    @property
    def _coll(self) -> Any:
        return self._mongo.db[Collection.PASSWORD_RESETS.value]

    def _decode(self, doc: dict[str, Any]) -> PasswordReset:
        return PasswordReset.model_validate(from_mongo_doc(doc))

    async def insert(self, reset: PasswordReset) -> None:
        try:
            doc = to_mongo_doc(reset.model_dump(mode="json"))
            await self._coll.insert_one(doc)
        except DuplicateKeyError as e:
            raise_on_dup_key(e, what=f"PasswordReset for user {reset.user_id}")
        except PyMongoError as e:
            raise_db_error(e, operation="password_reset.insert")

    async def get_by_token_hash(self, token_hash: str) -> PasswordReset | None:
        try:
            doc = await self._coll.find_one({"token_hash": token_hash})
        except PyMongoError as e:
            raise_db_error(e, operation="password_reset.get_by_token_hash")
        return self._decode(doc) if doc else None

    async def consume(self, reset_id: str) -> PasswordReset | None:
        try:
            doc = await self._coll.find_one_and_update(
                {"_id": reset_id, "consumed_at": None},
                {"$set": {"consumed_at": now_utc()}},
                return_document=True,
            )
        except PyMongoError as e:
            raise_db_error(e, operation="password_reset.consume", reset_id=reset_id)
        return self._decode(doc) if doc else None

    async def delete_pending_for_user(self, user_id: str) -> None:
        try:
            await self._coll.delete_many({"user_id": user_id, "consumed_at": None})
        except PyMongoError as e:
            raise_db_error(e, operation="password_reset.delete_pending_for_user")
