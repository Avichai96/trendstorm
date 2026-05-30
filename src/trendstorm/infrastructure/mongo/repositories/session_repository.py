"""MongoDB implementation of RefreshSessionRepository.

Mongo holds the audit copy. Redis holds the live lookup key (rt:{token_hash}).
This repo is for the security UI (list sessions, revoke by ID) and post-mortem.
"""

from __future__ import annotations

from typing import Any

from pymongo import DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError

from trendstorm.domain.sessions.models import RefreshSession
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


class MongoRefreshSessionRepository:
    """Not tenant-scoped — sessions are user-level."""

    def __init__(self, mongo: Any) -> None:
        self._mongo = mongo

    @property
    def _coll(self) -> Any:
        return self._mongo.db[Collection.REFRESH_SESSIONS.value]

    def _decode(self, doc: dict[str, Any]) -> RefreshSession:
        return RefreshSession.model_validate(from_mongo_doc(doc))

    async def insert(self, session: RefreshSession) -> None:
        try:
            doc = to_mongo_doc(session.model_dump(mode="json"))
            await self._coll.insert_one(doc)
        except DuplicateKeyError as e:
            raise_on_dup_key(e, what=f"RefreshSession for user {session.user_id}")
        except PyMongoError as e:
            raise_db_error(e, operation="session.insert")

    async def get_by_token_hash(self, token_hash: str) -> RefreshSession | None:
        try:
            doc = await self._coll.find_one({"refresh_token_hash": token_hash})
        except PyMongoError as e:
            raise_db_error(e, operation="session.get_by_token_hash")
        return self._decode(doc) if doc else None

    async def get(self, session_id: str) -> RefreshSession | None:
        try:
            doc = await self._coll.find_one({"_id": session_id})
        except PyMongoError as e:
            raise_db_error(e, operation="session.get", session_id=session_id)
        return self._decode(doc) if doc else None

    async def list_active_for_user(self, user_id: str) -> list[RefreshSession]:
        try:
            docs = (
                await self._coll.find({"user_id": user_id, "revoked_at": None})
                .sort("last_used_at", DESCENDING)
                .to_list(length=50)
            )
        except PyMongoError as e:
            raise_db_error(e, operation="session.list_active_for_user", user_id=user_id)
        return [self._decode(d) for d in docs]

    async def update_last_used(self, session_id: str) -> None:
        try:
            await self._coll.update_one(
                {"_id": session_id},
                {"$set": {"last_used_at": now_utc()}},
            )
        except PyMongoError as e:
            raise_db_error(e, operation="session.update_last_used", session_id=session_id)

    async def revoke(self, session_id: str) -> None:
        try:
            await self._coll.update_one(
                {"_id": session_id, "revoked_at": None},
                {"$set": {"revoked_at": now_utc()}},
            )
        except PyMongoError as e:
            raise_db_error(e, operation="session.revoke", session_id=session_id)

    async def revoke_all_for_user(self, user_id: str) -> None:
        try:
            await self._coll.update_many(
                {"user_id": user_id, "revoked_at": None},
                {"$set": {"revoked_at": now_utc()}},
            )
        except PyMongoError as e:
            raise_db_error(e, operation="session.revoke_all_for_user", user_id=user_id)
