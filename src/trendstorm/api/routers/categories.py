"""Categories router.

CRUD-ish endpoints for trend categories. Note: there's no DELETE — only
archive — because deletion would orphan jobs that referenced the category.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from trendstorm.api.deps import MongoDep
from trendstorm.domain.categories.models import Category
from trendstorm.infrastructure.mongo.repositories import MongoCategoryRepository
from trendstorm.services.category_service import CategoryService
from trendstorm.shared.ids import is_valid_id
from trendstorm.shared.logging import get_logger
from trendstorm.utils.headers_docs import require_tenant

logger = get_logger(__name__)

router = APIRouter(
    prefix="/v1/categories",
    tags=["categories"],
    dependencies=[Depends(require_tenant)]
)


# --- Schemas -----------------------------------------------------------

class CreateCategoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    keywords: list[str] = Field(default_factory=list)


class UpdateCategoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str | None = Field(default=None, max_length=2000)
    keywords: list[str] | None = None
    archived: bool | None = None


class CategoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    description: str | None
    keywords: list[str]
    archived: bool
    created_at: datetime
    updated_at: datetime


class CategoryListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    categories: list[CategoryResponse]
    next_cursor: str | None = None


def _to_response(cat: Category) -> CategoryResponse:
    return CategoryResponse(
        id=cat.id,
        name=cat.name,
        description=cat.description,
        keywords=cat.keywords,
        archived=cat.archived,
        created_at=cat.created_at,
        updated_at=cat.updated_at,
    )


# --- DI ----------------------------------------------------------------

def get_category_service(mongo: MongoDep) -> CategoryService:
    repo = MongoCategoryRepository(mongo)
    return CategoryService(categories=repo)


CategoryServiceDep = Annotated[CategoryService, Depends(get_category_service)]


# --- Endpoints ---------------------------------------------------------

@router.post(
    "",
    response_model=CategoryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a category",
)
async def create_category(
    request: Request,
    body: CreateCategoryRequest,
    service: CategoryServiceDep,
) -> CategoryResponse:
    category = await service.create_category(
        tenant_id=request.state.tenant_id,
        name=body.name,
        description=body.description,
        keywords=body.keywords,
    )
    return _to_response(category)


@router.get(
    "/{category_id}",
    response_model=CategoryResponse,
)
async def get_category(
    request: Request,
    category_id: Annotated[str, Path(min_length=26, max_length=26)],
    service: CategoryServiceDep,
) -> CategoryResponse:
    if not is_valid_id(category_id):
        from trendstorm.shared.errors import NotFoundError
        raise NotFoundError(f"Category {category_id} not found")
    category = await service.get_category(
        tenant_id=request.state.tenant_id,
        category_id=category_id,
    )
    return _to_response(category)


@router.patch(
    "/{category_id}",
    response_model=CategoryResponse,
)
async def update_category(
    request: Request,
    category_id: Annotated[str, Path(min_length=26, max_length=26)],
    body: UpdateCategoryRequest,
    service: CategoryServiceDep,
) -> CategoryResponse:
    category = await service.update_category(
        tenant_id=request.state.tenant_id,
        category_id=category_id,
        description=body.description,
        keywords=body.keywords,
        archived=body.archived,
    )
    return _to_response(category)


@router.get(
    "",
    response_model=CategoryListResponse,
)
async def list_categories(
    request: Request,
    service: CategoryServiceDep,
    include_archived: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    cursor: Annotated[str | None, Query(min_length=26, max_length=26)] = None,
) -> CategoryListResponse:
    cats, next_cursor = await service.list_categories(
        tenant_id=request.state.tenant_id,
        include_archived=include_archived,
        limit=limit,
        cursor=cursor,
    )
    return CategoryListResponse(
        categories=[_to_response(c) for c in cats],
        next_cursor=next_cursor,
    )
