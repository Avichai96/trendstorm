"""MongoDB implementation of UserRepository.

Users are NOT tenant-scoped — they are the root identity entity. This repo
does NOT extend TenantScopedRepository and does NOT use _tenant_query().
This is the documented exception to Rule 3 (alongside list_expired_pending
in review_repository.py and iter_completed in analysis_repository.py).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError

from trendstorm.domain.users.models import User
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


class MongoUserRepository:
    """Concrete UserRepository backed by MongoDB."""

    def __init__(self, mongo: Any) -> None:
        self._mongo = mongo

    @property
    def _coll(self) -> Any:
        return self._mongo.db[Collection.USERS.value]

    def _encode(self, user: User) -> dict[str, Any]:
        return to_mongo_doc(user.model_dump(mode="json"))

    def _decode(self, doc: dict[str, Any]) -> User:
        return User.model_validate(from_mongo_doc(doc))

    async def insert(self, user: User, *, session: Any | None = None) -> None:
        try:
            doc = self._encode(user)
            if session is not None:
                await self._coll.insert_one(doc, session=session)
            else:
                await self._coll.insert_one(doc)
        except DuplicateKeyError as e:
            raise_on_dup_key(e, what=f"User {user.email}")
        except PyMongoError as e:
            raise_db_error(e, operation="user.insert", email=user.email)

    async def get(self, user_id: str) -> User | None:
        try:
            doc = await self._coll.find_one({"_id": user_id})
        except PyMongoError as e:
            raise_db_error(e, operation="user.get", user_id=user_id)
        return self._decode(doc) if doc else None

    async def get_by_email(self, email: str) -> User | None:
        """Case-insensitive via the collation on users__email_unique index."""
        try:
            doc = await self._coll.find_one(
                {"email": email.lower()},
                collation={"locale": "en", "strength": 2},
            )
        except PyMongoError as e:
            raise_db_error(e, operation="user.get_by_email")
        return self._decode(doc) if doc else None

    async def get_by_subject(self, subject: str) -> User | None:
        try:
            doc = await self._coll.find_one({"identity_provider_subject": subject})
        except PyMongoError as e:
            raise_db_error(e, operation="user.get_by_subject")
        return self._decode(doc) if doc else None

    async def update(self, user: User) -> None:
        try:
            doc = self._encode(user)
            doc["updated_at"] = now_utc()
            doc.pop("_id", None)
            await self._coll.update_one({"_id": user.id}, {"$set": doc})
        except PyMongoError as e:
            raise_db_error(e, operation="user.update", user_id=user.id)

    async def tombstone(
        self, user_id: str, *, deleted_at: datetime, purge_at: datetime
    ) -> User | None:
        try:
            doc = await self._coll.find_one_and_update(
                {"_id": user_id, "deleted_at": None},
                {"$set": {"deleted_at": deleted_at, "purge_at": purge_at, "updated_at": now_utc()}},
                return_document=True,
            )
        except PyMongoError as e:
            raise_db_error(e, operation="user.tombstone", user_id=user_id)
        return self._decode(doc) if doc else None

    async def cancel_tombstone(self, user_id: str) -> User | None:
        try:
            doc = await self._coll.find_one_and_update(
                {"_id": user_id, "deleted_at": {"$ne": None}},
                {"$set": {"deleted_at": None, "purge_at": None, "updated_at": now_utc()}},
                return_document=True,
            )
        except PyMongoError as e:
            raise_db_error(e, operation="user.cancel_tombstone", user_id=user_id)
        return self._decode(doc) if doc else None

    async def list_due_for_purge(self, *, limit: int = 50) -> list[User]:
        now = datetime.now(UTC)
        try:
            docs = await self._coll.find(
                {"purge_at": {"$lte": now}, "deleted_at": {"$ne": None}}
            ).sort("purge_at", ASCENDING).limit(limit).to_list(length=limit)
        except PyMongoError as e:
            raise_db_error(e, operation="user.list_due_for_purge")
        return [self._decode(d) for d in docs]

    async def hard_delete(self, user_id: str) -> None:
        try:
            await self._coll.delete_one({"_id": user_id})
        except PyMongoError as e:
            raise_db_error(e, operation="user.hard_delete", user_id=user_id)

    async def set_email_verified(self, user_id: str) -> User | None:
        try:
            doc = await self._coll.find_one_and_update(
                {"_id": user_id},
                {"$set": {"email_verified": True, "updated_at": now_utc()}},
                return_document=True,
            )
        except PyMongoError as e:
            raise_db_error(e, operation="user.set_email_verified", user_id=user_id)
        return self._decode(doc) if doc else None
