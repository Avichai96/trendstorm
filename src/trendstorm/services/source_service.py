"""Source use cases.

Notable rule: registering a source REQUIRES the category to exist within
the same tenant. We always verify by looking up the category first.
This prevents orphan sources from cluttering the DB.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opentelemetry import trace

from trendstorm.domain.sources.models import Source
from trendstorm.shared.errors import ConflictError, NotFoundError, ValidationError
from trendstorm.shared.logging import get_logger
from trendstorm.shared.types import SourceType

if TYPE_CHECKING:
    from trendstorm.domain.categories.repository import CategoryRepository
    from trendstorm.domain.sources.repository import SourceRepository


logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)


# Per-category soft cap. Phase 12 can move this to per-tenant plan limits.
MAX_SOURCES_PER_CATEGORY = 500


class SourceService:
    """Service for Source use cases."""

    def __init__(
        self,
        *,
        sources: SourceRepository,
        categories: CategoryRepository,
    ) -> None:
        self._sources = sources
        self._categories = categories

    async def register_source(
        self,
        *,
        tenant_id: str,
        category_id: str,
        url: str,
        label: str | None = None,
        source_type: SourceType = SourceType.HTTP,
    ) -> Source:
        """Register a new source under a category.

        Validation order matters:
            1. Category exists?  -> 404 if not
            2. Category at cap?  -> 422 if so
            3. URL valid?        -> caught by Source's field_validator
            4. Duplicate URL?    -> 409 from unique index
        """
        with tracer.start_as_current_span("source.register"):
            # 1. Category must exist in this tenant.
            category = await self._categories.get(tenant_id, category_id)
            if category is None:
                raise NotFoundError(
                    f"Category {category_id} not found",
                    context={"category_id": category_id},
                )

            # 2. Cap check. We do a cheap count rather than an arithmetic
            # counter for now — the cap is loose and reads are infrequent.
            existing = await self._sources.list_by_category(
                tenant_id,
                category_id,
                enabled_only=False,
                limit=MAX_SOURCES_PER_CATEGORY + 1,
            )
            if len(existing) >= MAX_SOURCES_PER_CATEGORY:
                raise ValidationError(
                    f"Category has reached the {MAX_SOURCES_PER_CATEGORY}-source limit",
                    code="category_source_limit",
                    context={"limit": MAX_SOURCES_PER_CATEGORY},
                )

            # 3+4. Build + insert. The model validator catches bad URLs;
            # the unique index catches duplicates -> mapped to ConflictError.
            try:
                source = Source(
                    tenant_id=tenant_id,
                    category_id=category_id,
                    url=url,
                    label=label,
                    type=source_type,
                )
            except ValueError as e:
                raise ValidationError(
                    f"Invalid URL: {e}",
                    code="invalid_url",
                    context={"url": url},
                ) from e

            try:
                await self._sources.insert(source)
            except ConflictError:
                # The dup index uses (tenant_id, url_hash). If we hit it,
                # tell the user which source they're trying to re-add.
                raise ConflictError(
                    f"Source {source.url} is already registered in this tenant",
                    code="source_url_duplicate",
                    context={"url": source.url},
                ) from None

            logger.info(
                "source_registered",
                source_id=source.id,
                category_id=category_id,
                url=source.url,
            )
            return source

    async def get_source(
        self,
        *,
        tenant_id: str,
        source_id: str,
    ) -> Source:
        source = await self._sources.get(tenant_id, source_id)
        if source is None:
            raise NotFoundError(f"Source {source_id} not found")
        return source

    async def list_sources(
        self,
        *,
        tenant_id: str,
        category_id: str,
        enabled_only: bool = False,
    ) -> list[Source]:
        # Validate the category exists; gives a nicer 404 than empty list.
        if await self._categories.get(tenant_id, category_id) is None:
            raise NotFoundError(f"Category {category_id} not found")
        return await self._sources.list_by_category(
            tenant_id,
            category_id,
            enabled_only=enabled_only,
        )

    async def disable_source(
        self,
        *,
        tenant_id: str,
        source_id: str,
    ) -> None:
        """Soft-delete a source (set enabled=False). Raises NotFoundError if missing."""
        with tracer.start_as_current_span("source.disable"):
            result = await self._sources.disable(tenant_id, source_id)
            if result is None:
                raise NotFoundError(f"Source {source_id} not found")
            logger.info("source.disabled", tenant_id=tenant_id, source_id=source_id)
