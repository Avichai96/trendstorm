"""Category use cases.

The service layer turns single-purpose repository calls into business
operations. Routers should never call repositories directly — they go
through the service.

Operations:
    - `create_category`: validate uniqueness, then insert.
    - `update_category`: partial update with permission checks.
    - `archive_category`: soft delete.
    - `list_categories`: paginated list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opentelemetry import trace

from trendstorm.domain.categories.models import Category
from trendstorm.shared.errors import ConflictError, NotFoundError
from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    from trendstorm.domain.categories.repository import CategoryRepository


logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)


class CategoryService:
    """Service for Category use cases."""

    def __init__(self, *, categories: CategoryRepository) -> None:
        self._categories = categories

    async def create_category(
        self,
        *,
        tenant_id: str,
        name: str,
        description: str | None = None,
        keywords: list[str] | None = None,
    ) -> Category:
        """Create a new category. Raises ConflictError if name collides.

        We do an explicit `get_by_name` BEFORE insert for a friendlier
        error message. The unique index would catch it anyway, but
        "category 'Crypto' already exists" beats "duplicate key error
        index: tenant_name_unique".
        """
        with tracer.start_as_current_span("category.create"):
            existing = await self._categories.get_by_name(tenant_id, name)
            if existing is not None:
                raise ConflictError(
                    f"Category {name!r} already exists",
                    code="category_name_taken",
                    context={"existing_id": existing.id},
                )

            category = Category(
                tenant_id=tenant_id,
                name=name,
                description=description,
                keywords=keywords or [],
            )
            await self._categories.insert(category)
            logger.info(
                "category_created",
                category_id=category.id,
                name=category.name,
            )
            return category

    async def update_category(
        self,
        *,
        tenant_id: str,
        category_id: str,
        description: str | None = None,
        keywords: list[str] | None = None,
        archived: bool | None = None,
    ) -> Category:
        """Update a category. Raises NotFoundError if missing."""
        with tracer.start_as_current_span("category.update"):
            updated = await self._categories.update(
                tenant_id,
                category_id,
                description=description,
                keywords=keywords,
                archived=archived,
            )
            if updated is None:
                raise NotFoundError(
                    f"Category {category_id} not found",
                    context={"category_id": category_id},
                )
            return updated

    async def archive_category(
        self,
        *,
        tenant_id: str,
        category_id: str,
    ) -> Category:
        """Soft-delete the category.

        The category is hidden from default lists but kept for historical
        job references.
        """
        return await self.update_category(
            tenant_id=tenant_id,
            category_id=category_id,
            archived=True,
        )

    async def get_category(
        self,
        *,
        tenant_id: str,
        category_id: str,
    ) -> Category:
        """Get one or raise NotFoundError."""
        category = await self._categories.get(tenant_id, category_id)
        if category is None:
            raise NotFoundError(f"Category {category_id} not found")
        return category

    async def list_categories(
        self,
        *,
        tenant_id: str,
        include_archived: bool = False,
        limit: int = 100,
        cursor: str | None = None,
    ) -> tuple[list[Category], str | None]:
        return await self._categories.list_by_tenant(
            tenant_id,
            include_archived=include_archived,
            limit=limit,
            cursor=cursor,
        )
