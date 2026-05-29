"""Shared utilities for Mongo repositories.

Two kinds of helpers live here:

1. Pure functions (no state):
    - `now_utc()`, `to_mongo_doc()`, `from_mongo_doc()`, `raise_on_dup_key()`

2. A mixin `TenantScopedRepository` that individual repositories COMPOSE.
   It provides:
    - Boilerplate-free `_coll` accessor.
    - A `_tenant_query()` helper that EVERY query goes through.
    - Pydantic encoding/decoding.

Why a mixin and not a "real" base class with CRUD methods?

   A generic `BaseRepository[T].find()` looks tidy but trains engineers
   to think of repositories as a thin facade over Mongo. That's the
   opposite of what a repository is for. A good repository method reads
   like business intent: `list_pending_jobs_for_tenant()`, not
   `find({"tenant_id": t, "status": "pending"})`. The mixin gives shared
   plumbing without dictating the public method shape.

What the mixin DOES enforce:
    - The collection name is declared once, as a class attribute.
    - The Pydantic model is declared once, as a generic parameter.
    - `_tenant_query(tenant_id, **extra)` is the ONLY way to start a query.
      It guarantees `tenant_id` is in every filter — defense in depth.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel
from pymongo.errors import DuplicateKeyError, PyMongoError

from trendstorm.infrastructure.mongo.client import MongoClient
from trendstorm.infrastructure.mongo.schema import Collection
from trendstorm.shared.errors import ConflictError, DatabaseError

if TYPE_CHECKING:
    from motor.motor_asyncio import AsyncIOMotorCollection


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def now_utc() -> datetime:
    """Return timezone-aware UTC now.

    NEVER use naive datetimes — they silently break time comparisons against
    TZ-aware values.
    """
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Document <-> model translation
# ---------------------------------------------------------------------------


def to_mongo_doc(model_dump: dict[str, Any]) -> dict[str, Any]:
    """Translate a Pydantic model_dump into a Mongo document.

    Convention: `id` field becomes Mongo's `_id`. This keeps domain models
    free of Mongo-specific naming — `_id` never appears in Pydantic models
    or API responses.
    """
    doc = dict(model_dump)
    if "id" in doc:
        doc["_id"] = doc.pop("id")
    return doc


def from_mongo_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """Translate a Mongo document back into a Pydantic-loadable dict."""
    out = dict(doc)
    if "_id" in out:
        out["id"] = out.pop("_id")
    return out


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def raise_on_dup_key(err: DuplicateKeyError, *, what: str) -> None:
    """Translate pymongo's DuplicateKeyError to our domain ConflictError."""
    raise ConflictError(
        f"{what} already exists",
        code="duplicate_key",
        context={"mongo_detail": str(err.details) if err.details else ""},
    ) from err


def raise_db_error(err: PyMongoError, *, operation: str, **context: Any) -> None:
    """Wrap a pymongo error as our DatabaseError with structured context."""
    raise DatabaseError(
        f"{operation} failed",
        context={"error": str(err), "error_type": type(err).__name__, **context},
    ) from err


# ---------------------------------------------------------------------------
# Tenant-scoped repository mixin
# ---------------------------------------------------------------------------


class TenantScopedRepository[T: BaseModel]:
    """Mixin providing tenant-safe primitives for repositories.

    Subclasses MUST declare:
        collection: ClassVar[Collection]
        model:      ClassVar[type[T]]

    Subclasses then implement domain-meaningful methods using
    `_tenant_query()` to construct safe filters, and `_decode()` /
    `_encode()` for Pydantic <-> Mongo conversion.

    Example:
        class MyRepo(TenantScopedRepository[MyModel]):
            collection = Collection.MY_THING
            model = MyModel

            async def list_active(self, tenant_id: str) -> list[MyModel]:
                docs = await self._find(
                    self._tenant_query(tenant_id, active=True)
                )
                return [self._decode(d) for d in docs]

    """

    # Subclasses MUST override these:
    collection: ClassVar[Collection]
    model: ClassVar[type[BaseModel]]

    def __init__(self, mongo: MongoClient) -> None:
        self._mongo = mongo

    # ----- accessors -------------------------------------------------------

    @property
    def _coll(self) -> AsyncIOMotorCollection:  # type: ignore[type-arg]  # motor stubs lack precise generic params
        return self._mongo.db[self.collection.value]

    # ----- query builders -------------------------------------------------

    @staticmethod
    def _tenant_query(tenant_id: str, /, **extra: Any) -> dict[str, Any]:
        """Build a Mongo filter that ALWAYS includes tenant_id.

        Usage:
            self._tenant_query(t, status="active", _id=some_id)
            -> {"tenant_id": t, "status": "active", "_id": some_id}

        This is the ONLY function in the codebase that should construct
        Mongo filters for tenant-scoped collections. By funneling every
        query through it, we guarantee no method can accidentally skip
        the tenant scope.
        """
        if not tenant_id:
            raise ValueError("tenant_id is required for tenant-scoped queries")
        return {"tenant_id": tenant_id, **extra}

    # ----- encode/decode --------------------------------------------------

    def _encode(self, instance: T) -> dict[str, Any]:
        return to_mongo_doc(instance.model_dump(mode="json"))

    def _decode(self, doc: dict[str, Any]) -> T:
        return self.model.model_validate(from_mongo_doc(doc))  # type: ignore[return-value]

    # ----- safe primitives ------------------------------------------------
    # These wrap pymongo errors -> DatabaseError. Subclasses use them.

    async def _insert(
        self,
        doc: dict[str, Any],
        *,
        what: str,
        session: Any | None = None,
    ) -> None:
        try:
            if session is not None:
                await self._coll.insert_one(doc, session=session)
            else:
                await self._coll.insert_one(doc)
        except DuplicateKeyError as e:
            raise_on_dup_key(e, what=what)
        except PyMongoError as e:
            raise_db_error(e, operation=f"insert {what}")

    async def _find_one(self, query: dict[str, Any], *, what: str) -> dict[str, Any] | None:
        try:
            return await self._coll.find_one(query)
        except PyMongoError as e:
            raise_db_error(e, operation=f"find_one {what}", query=query)
            return None  # unreachable; mypy doesn't know raise_db_error is NoReturn

    async def _find_many(
        self,
        query: dict[str, Any],
        *,
        sort: list[tuple[str, int]] | None = None,
        limit: int | None = None,
        what: str,
    ) -> list[dict[str, Any]]:
        try:
            cursor = self._coll.find(query)
            if sort is not None:
                cursor = cursor.sort(sort)
            if limit is not None:
                cursor = cursor.limit(limit)
            return await cursor.to_list(length=limit)
        except PyMongoError as e:
            raise_db_error(e, operation=f"find_many {what}", query=query)
            return []  # unreachable
