"""Organizations router — tenant-scoped org management.

POST /v1/organizations         — create a new org (the calling user becomes OWNER)
GET  /v1/organizations/current — get the active org for the session
PATCH /v1/organizations/current — update name / billing_email
POST /v1/organizations/switch  — switch the session's active tenant_id
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from trendstorm.api.deps import MongoDep
from trendstorm.domain.auth.models import AuthContext
from trendstorm.domain.memberships.models import Membership, Role
from trendstorm.domain.organizations.models import Organization
from trendstorm.infrastructure.mongo.repositories.membership_repository import (
    MongoMembershipRepository,
)
from trendstorm.infrastructure.mongo.repositories.organization_repository import (
    MongoOrganizationRepository,
)
from trendstorm.infrastructure.mongo.repositories.session_repository import (
    MongoRefreshSessionRepository,
)
from trendstorm.services.auth.session_service import SessionService
from trendstorm.shared.config import get_settings
from trendstorm.shared.errors import AuthorizationError, NotFoundError
from trendstorm.shared.logging import get_logger
from trendstorm.utils.headers_docs import require_tenant

logger = get_logger(__name__)

router = APIRouter(
    prefix="/v1/organizations",
    tags=["organizations"],
    dependencies=[Depends(require_tenant)],
)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class OrgResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    slug: str
    plan: str
    billing_email: str
    owner_user_id: str | None


class CreateOrgRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    slug: str = Field(min_length=2, max_length=48, pattern=r"^[a-z0-9-]+$")
    billing_email: str


class UpdateOrgRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=80)
    billing_email: str | None = None


class SwitchOrgRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    org_id: str


def _to_response(org: Organization) -> OrgResponse:
    return OrgResponse(
        id=org.id,
        name=org.name,
        slug=org.slug,
        plan=org.plan,
        billing_email=org.billing_email,
        owner_user_id=org.owner_user_id,
    )


def _auth_context(request: Request) -> AuthContext:
    ctx = getattr(request.state, "auth_context", None)
    if ctx is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated.")
    return ctx  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_organization(
    body: CreateOrgRequest, request: Request, mongo: MongoDep
) -> OrgResponse:
    ctx = _auth_context(request)
    if ctx.user_id is None:
        raise AuthorizationError("API key auth cannot create organizations.")
    org_repo = MongoOrganizationRepository(mongo)
    membership_repo = MongoMembershipRepository(mongo)

    org = Organization(
        name=body.name,
        slug=body.slug,
        owner_user_id=ctx.user_id,
        billing_email=body.billing_email,
    )
    await org_repo.insert(org)
    membership = Membership(
        tenant_id=org.id,
        user_id=ctx.user_id,
        roles=[Role.OWNER],
    )
    await membership_repo.insert(membership)
    logger.info("organization.created", org_id=org.id, user_id=ctx.user_id)
    return _to_response(org)


@router.get("/current")
async def get_current_org(request: Request, mongo: MongoDep) -> OrgResponse:
    ctx = _auth_context(request)
    org_repo = MongoOrganizationRepository(mongo)
    org = await org_repo.get(ctx.tenant_id)
    if org is None:
        raise NotFoundError("Organization not found.")
    return _to_response(org)


@router.patch("/current")
async def update_current_org(
    body: UpdateOrgRequest, request: Request, mongo: MongoDep
) -> OrgResponse:
    ctx = _auth_context(request)
    if ctx.user_id is None:
        raise AuthorizationError("API key auth cannot update organization.")
    membership_repo = MongoMembershipRepository(mongo)
    membership = await membership_repo.get_for_user(ctx.tenant_id, ctx.user_id)
    if membership is None or not membership.is_admin_or_above:
        raise AuthorizationError("Admin role required to update organization.")

    org_repo = MongoOrganizationRepository(mongo)
    org = await org_repo.get(ctx.tenant_id)
    if org is None:
        raise NotFoundError("Organization not found.")

    updated = org.model_copy(update={
        k: v for k, v in {"name": body.name, "billing_email": body.billing_email}.items()
        if v is not None
    })
    await org_repo.update(updated)
    return _to_response(updated)


@router.post("/switch", status_code=status.HTTP_200_OK)
async def switch_org(
    body: SwitchOrgRequest,
    request: Request,
    response: Response,
    mongo: MongoDep,
) -> dict[str, Any]:
    ctx = _auth_context(request)
    if ctx.user_id is None:
        raise AuthorizationError("API key auth cannot switch organization.")
    membership_repo = MongoMembershipRepository(mongo)
    membership = await membership_repo.get_for_user(body.org_id, ctx.user_id)
    if membership is None:
        raise AuthorizationError("You are not a member of that organization.")

    settings = get_settings()
    from trendstorm.infrastructure.mongo.repositories.user_repository import MongoUserRepository

    user_repo = MongoUserRepository(mongo)
    redis = request.app.state.redis
    session_repo = MongoRefreshSessionRepository(mongo)
    session_svc = SessionService(
        session_repo=session_repo,
        user_repo=user_repo,
        membership_repo=membership_repo,
        redis=redis,
        jwt_settings=settings.jwt,
    )
    access_jwt = await session_svc.issue_access_jwt_for_org_switch(ctx.user_id, body.org_id)
    return {"access_token": access_jwt, "tenant_id": body.org_id}
