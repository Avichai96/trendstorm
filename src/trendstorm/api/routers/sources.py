"""Sources router."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from trendstorm.api.deps import MongoDep
from trendstorm.domain.sources.models import Source
from trendstorm.infrastructure.mongo.repositories import (
    MongoCategoryRepository,
    MongoSourceRepository,
)
from trendstorm.services.source_service import SourceService
from trendstorm.shared.ids import is_valid_id
from trendstorm.shared.types import SourceType

router = APIRouter(prefix="/v1/sources", tags=["sources"])


# --- Schemas -----------------------------------------------------------

class RegisterSourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_id: str = Field(..., min_length=26, max_length=26)
    url: str = Field(..., min_length=4, max_length=4096)
    label: str | None = Field(default=None, max_length=200)
    type: SourceType = SourceType.HTTP


class SourceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    category_id: str
    url: str
    label: str | None
    type: SourceType
    enabled: bool
    last_fetch_at: datetime | None
    last_fetch_status: str | None
    last_fetch_error: str | None
    created_at: datetime


class SourceListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: list[SourceResponse]


def _to_response(s: Source) -> SourceResponse:
    return SourceResponse(
        id=s.id,
        category_id=s.category_id,
        url=s.url,
        label=s.label,
        type=s.type,
        enabled=s.enabled,
        last_fetch_at=s.last_fetch_at,
        last_fetch_status=s.last_fetch_status,
        last_fetch_error=s.last_fetch_error,
        created_at=s.created_at,
    )


# --- DI ----------------------------------------------------------------

def get_source_service(mongo: MongoDep) -> SourceService:
    return SourceService(
        sources=MongoSourceRepository(mongo),
        categories=MongoCategoryRepository(mongo),
    )


SourceServiceDep = Annotated[SourceService, Depends(get_source_service)]


# --- Endpoints ---------------------------------------------------------

@router.post(
    "",
    response_model=SourceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_source(
    request: Request,
    body: RegisterSourceRequest,
    service: SourceServiceDep,
) -> SourceResponse:
    source = await service.register_source(
        tenant_id=request.state.tenant_id,
        category_id=body.category_id,
        url=body.url,
        label=body.label,
        source_type=body.type,
    )
    return _to_response(source)


@router.get(
    "/{source_id}",
    response_model=SourceResponse,
)
async def get_source(
    request: Request,
    source_id: Annotated[str, Path(min_length=26, max_length=26)],
    service: SourceServiceDep,
) -> SourceResponse:
    if not is_valid_id(source_id):
        from trendstorm.shared.errors import NotFoundError
        raise NotFoundError(f"Source {source_id} not found")
    source = await service.get_source(
        tenant_id=request.state.tenant_id,
        source_id=source_id,
    )
    return _to_response(source)


@router.get(
    "",
    response_model=SourceListResponse,
)
async def list_sources(
    request: Request,
    service: SourceServiceDep,
    category_id: Annotated[str, Query(min_length=26, max_length=26)],
    enabled_only: Annotated[bool, Query()] = False,
) -> SourceListResponse:
    sources = await service.list_sources(
        tenant_id=request.state.tenant_id,
        category_id=category_id,
        enabled_only=enabled_only,
    )
    return SourceListResponse(sources=[_to_response(s) for s in sources])
