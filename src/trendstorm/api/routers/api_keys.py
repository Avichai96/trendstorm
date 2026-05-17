"""API key management router.

Endpoints for provisioning, listing, and revoking per-tenant API keys.

The raw plaintext key is returned ONCE on creation and cannot be recovered
later. All subsequent responses include only the key_prefix (first 8 chars
of the random portion) for display purposes (e.g. "ts_live_Ab3cXy7z…").

POST   /v1/api-keys           — create a new key (returns plaintext once)
GET    /v1/api-keys           — list all keys (active + revoked)
DELETE /v1/api-keys/{key_id}  — revoke a key immediately
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, status
from pydantic import BaseModel, ConfigDict, Field

from trendstorm.api.deps import AuthServiceDep
from trendstorm.utils.headers_docs import require_tenant

router = APIRouter(
    prefix="/v1/api-keys",
    tags=["api-keys"],
    dependencies=[Depends(require_tenant)],
)


# --- Schemas -----------------------------------------------------------

class CreateApiKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=100)


class ApiKeyCreatedResponse(BaseModel):
    """Returned once on creation — includes the plaintext key."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    key: str                        # plaintext; shown ONCE
    key_prefix: str                 # for display only
    tenant_id: str
    created_at: datetime


class ApiKeyResponse(BaseModel):
    """Standard response — plaintext key never included."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    key_prefix: str
    tenant_id: str
    created_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    is_active: bool


class ApiKeyListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keys: list[ApiKeyResponse]


# --- Endpoints ---------------------------------------------------------

@router.post(
    "",
    response_model=ApiKeyCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an API key",
)
async def create_api_key(
    request: Request,
    body: CreateApiKeyRequest,
    auth_svc: AuthServiceDep,
) -> ApiKeyCreatedResponse:
    api_key, raw_key = await auth_svc.create_key(
        tenant_id=request.state.tenant_id,
        name=body.name,
    )
    return ApiKeyCreatedResponse(
        id=api_key.id,
        name=api_key.name,
        key=raw_key,
        key_prefix=api_key.key_prefix,
        tenant_id=api_key.tenant_id,
        created_at=api_key.created_at,
    )


@router.get(
    "",
    response_model=ApiKeyListResponse,
    summary="List API keys",
)
async def list_api_keys(
    request: Request,
    auth_svc: AuthServiceDep,
) -> ApiKeyListResponse:
    keys = await auth_svc.list_keys(tenant_id=request.state.tenant_id)
    return ApiKeyListResponse(
        keys=[
            ApiKeyResponse(
                id=k.id,
                name=k.name,
                key_prefix=k.key_prefix,
                tenant_id=k.tenant_id,
                created_at=k.created_at,
                last_used_at=k.last_used_at,
                revoked_at=k.revoked_at,
                is_active=k.is_active,
            )
            for k in keys
        ]
    )


@router.delete(
    "/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an API key",
)
async def revoke_api_key(
    request: Request,
    key_id: Annotated[str, Path(min_length=26, max_length=26)],
    auth_svc: AuthServiceDep,
) -> None:
    await auth_svc.revoke_key(
        tenant_id=request.state.tenant_id,
        key_id=key_id,
    )
