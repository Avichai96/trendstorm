"""MongoDB implementation of CategoryRepository."""
from __future__ import annotations

from typing import ClassVar

from pymongo.errors import PyMongoError

from trendstorm.domain.categories.models import Category
from trendstorm.infrastructure.mongo.repositories._base import (
    TenantScopedRepository,
    now_utc,
    raise_db_error,
)
from trendstorm.infrastructure.mongo.schema import Collection
from trendstorm.shared.logging import get_logger

logger = get_logger(__name__)


class MongoCategoryRepository(TenantScopedRepository[Category]):
    """Concrete CategoryRepository backed by MongoDB."""

    collection: ClassVar[Collection] = Collection.CATEGORIES
    model: ClassVar[type[Category]] = Category

    async def insert(self, category: Category) -> None:
        await self._insert(self._encode(category), what=f"Category {category.name!r}")

    async def get(self, tenant_id: str, category_id: str) -> Category | None:
        doc = await self._find_one(
            self._tenant_query(tenant_id, _id=category_id),
            what=f"Category {category_id}",
        )
        return self._decode(doc) if doc else None

    async def get_by_name(self, tenant_id: str, name: str) -> Category | None:
        """Lookup by canonical name. Used by upsert flows and the API.

        The name is matched after `.strip()` (mirroring the model validator)
        so trailing whitespace inputs hit the same row.
        """
        doc = await self._find_one(
            self._tenant_query(tenant_id, name=name.strip()),
            what=f"Category name={name!r}",
        )
        return self._decode(doc) if doc else None

    async def list_by_tenant(
        self,
        tenant_id: str,
        *,
        include_archived: bool = False,
        limit: int = 100,
        cursor: str | None = None,
    ) -> tuple[list[Category], str | None]:
        query = self._tenant_query(tenant_id)
        if not include_archived:
            query["archived"] = False
        if cursor is not None:
            query["_id"] = {"$lt": cursor}

        docs = await self._find_many(
            query,
            sort=[("_id", -1)],
            limit=limit + 1,
            what="categories list",
        )
        has_more = len(docs) > limit
        docs = docs[:limit]
        cats = [self._decode(d) for d in docs]
        next_cursor = cats[-1].id if has_more and cats else None
        return cats, next_cursor

    async def update(
        self,
        tenant_id: str,
        category_id: str,
        *,
        description: str | None = None,
        keywords: list[str] | None = None,
        archived: bool | None = None,
    ) -> Category | None:
        """Partial update — only fields explicitly passed are changed.

        Uses `find_one_and_update` to return the updated document atomically.
        Without that, a separate `find_one` after the update could see a
        concurrent modification by another writer.
        """
        update_set: dict[str, object] = {"updated_at": now_utc()}
        if description is not None:
            update_set["description"] = description
        if keywords is not None:
            update_set["keywords"] = keywords
        if archived is not None:
            update_set["archived"] = archived

        if len(update_set) == 1:  # only updated_at — caller passed nothing
            return await self.get(tenant_id, category_id)

        try:
            doc = await self._coll.find_one_and_update(
                self._tenant_query(tenant_id, _id=category_id),
                {"$set": update_set},
                return_document=True,    # returnDocument=AFTER
            )
        except PyMongoError as e:
            raise_db_error(e, operation="update category", category_id=category_id)
        return self._decode(doc) if doc else None

    async def count_by_tenant(self, tenant_id: str) -> int:
        """Count non-archived categories. Used by quota checks + dashboards."""
        try:
            return await self._coll.count_documents(
                self._tenant_query(tenant_id, archived=False)
            )
        except PyMongoError as e:
            raise_db_error(e, operation="count categories", tenant_id=tenant_id)
            return 0  # unreachable
