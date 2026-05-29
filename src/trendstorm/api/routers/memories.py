"""Memories router — user-curated long-term memory management.

POST   /v1/categories/{category_id}/memories        Create a user-curated memory.
GET    /v1/categories/{category_id}/memories        List active memories for a category.
DELETE /v1/categories/{category_id}/memories/{id}   Deactivate a memory (soft delete).

All routes require the "tenant_admin" role — curating memories is a privileged
operation because memories are injected into future analyst contexts.

Memory pipeline note: episodic and semantic memories are auto-created by the
memory-consolidation-worker. This router only manages user-curated memories.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, status
from pydantic import BaseModel, ConfigDict, Field

from trendstorm.api.deps import MongoDep
from trendstorm.domain.memories.models import Memory, MemoryKind, MemorySource
from trendstorm.infrastructure.mongo.repositories import MongoMemoryRepository
from trendstorm.infrastructure.mongo.repositories._base import now_utc
from trendstorm.shared.errors import NotFoundError
from trendstorm.shared.ids import new_id
from trendstorm.shared.logging import get_logger
from trendstorm.utils.headers_docs import require_role, require_tenant

logger = get_logger(__name__)

router = APIRouter(
    prefix="/v1/categories/{category_id}/memories",
    tags=["memories"],
    dependencies=[Depends(require_tenant), require_role("tenant_admin")],
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class CreateMemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=4000)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    tags: list[str] = Field(default_factory=list)
    curated_by: str = Field(min_length=1, max_length=256)


class MemoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    tenant_id: str
    category_id: str
    kind: str
    source: str
    content: str
    confidence: float
    is_active: bool
    tags: list[str]
    superseded_by: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_domain(cls, m: Memory) -> "MemoryResponse":
        return cls(
            id=m.id,
            tenant_id=m.tenant_id,
            category_id=m.category_id,
            kind=m.kind.value,
            source=m.source.value,
            content=m.content,
            confidence=m.confidence,
            is_active=m.is_active,
            tags=m.tags,
            superseded_by=m.superseded_by,
            created_at=m.created_at.isoformat(),
            updated_at=m.updated_at.isoformat(),
        )


class MemoryListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[MemoryResponse]
    total: int


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

CategoryIdPath = Annotated[str, Path(description="Category ULID")]
MemoryIdPath = Annotated[str, Path(description="Memory ULID")]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=MemoryResponse,
    summary="Create a user-curated memory",
)
async def create_memory(
    request: Request,
    category_id: CategoryIdPath,
    body: CreateMemoryRequest,
    mongo: MongoDep,
) -> MemoryResponse:
    tenant_id: str = request.state.tenant_id
    now = now_utc()
    memory = Memory(
        id=new_id(),
        tenant_id=tenant_id,
        category_id=category_id,
        kind=MemoryKind.SEMANTIC,
        source=MemorySource.USER_CURATED,
        content=body.content,
        confidence=body.confidence,
        source_job_id="user_curated",
        source_analysis_id="user_curated",
        tags=body.tags,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    repo = MongoMemoryRepository(mongo)
    await repo.insert(memory)
    logger.info("memory.user_curated.created", memory_id=memory.id, category_id=category_id)
    return MemoryResponse.from_domain(memory)


@router.get(
    "",
    response_model=MemoryListResponse,
    summary="List active memories for a category",
)
async def list_memories(
    request: Request,
    category_id: CategoryIdPath,
    mongo: MongoDep,
) -> MemoryListResponse:
    tenant_id: str = request.state.tenant_id
    repo = MongoMemoryRepository(mongo)
    memories = await repo.list_active_for_category(tenant_id, category_id)
    return MemoryListResponse(
        items=[MemoryResponse.from_domain(m) for m in memories],
        total=len(memories),
    )


@router.delete(
    "/{memory_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Deactivate a memory (soft delete)",
)
async def delete_memory(
    request: Request,
    category_id: CategoryIdPath,
    memory_id: MemoryIdPath,
    mongo: MongoDep,
) -> None:
    tenant_id: str = request.state.tenant_id
    repo = MongoMemoryRepository(mongo)
    memory = await repo.get(tenant_id, memory_id)
    if memory is None or memory.category_id != category_id:
        raise NotFoundError(
            f"Memory {memory_id} not found",
            context={"memory_id": memory_id, "category_id": category_id},
        )
    await repo.deactivate(tenant_id, memory_id)
    logger.info("memory.user_curated.deleted", memory_id=memory_id, category_id=category_id)
