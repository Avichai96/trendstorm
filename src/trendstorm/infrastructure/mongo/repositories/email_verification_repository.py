"""MongoDB implementation of EmailVerificationRepository."""

from __future__ import annotations

from typing import Any

from pymongo.errors import DuplicateKeyError, PyMongoError

from trendstorm.domain.email_verifications.models import EmailVerification
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


class MongoEmailVerificationRepository:
    """Not tenant-scoped — users don't have a tenant_id."""

    def __init__(self, mongo: Any) -> None:
        self._mongo = mongo

    @property
    def _coll(self) -> Any:
        return self._mongo.db[Collection.EMAIL_VERIFICATIONS.value]

    def _decode(self, doc: dict[str, Any]) -> EmailVerification:
        return EmailVerification.model_validate(from_mongo_doc(doc))

    async def insert(self, verification: EmailVerification) -> None:
        try:
            doc = to_mongo_doc(verification.model_dump(mode="json"))
            await self._coll.insert_one(doc)
        except DuplicateKeyError as e:
            raise_on_dup_key(e, what=f"EmailVerification for user {verification.user_id}")
        except PyMongoError as e:
            raise_db_error(e, operation="email_verification.insert")

    async def get_by_token_hash(self, token_hash: str) -> EmailVerification | None:
        try:
            doc = await self._coll.find_one({"token_hash": token_hash})
        except PyMongoError as e:
            raise_db_error(e, operation="email_verification.get_by_token_hash")
        return self._decode(doc) if doc else None

    async def consume(self, verification_id: str) -> EmailVerification | None:
        try:
            doc = await self._coll.find_one_and_update(
                {"_id": verification_id, "consumed_at": None},
                {"$set": {"consumed_at": now_utc()}},
                return_document=True,
            )
        except PyMongoError as e:
            raise_db_error(e, operation="email_verification.consume", vid=verification_id)
        return self._decode(doc) if doc else None

    async def delete_pending_for_user(self, user_id: str) -> None:
        try:
            await self._coll.delete_many({"user_id": user_id, "consumed_at": None})
        except PyMongoError as e:
            raise_db_error(e, operation="email_verification.delete_pending_for_user")
