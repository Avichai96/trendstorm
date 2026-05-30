"""Invites router.

POST   /v1/invites                        — send invite (admin)
GET    /v1/invites                        — list pending invites (admin)
DELETE /v1/invites/{id}                   — revoke invite (admin)
POST   /v1/invites/{id}/resend            — resend invite (admin)
GET    /v1/invites/by-token/{token}       — preview invite (public)
POST   /v1/invites/by-token/{token}/accept — accept invite (authenticated user)
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from trendstorm.api.deps import MongoDep, RedisDep
from trendstorm.domain.auth.models import AuthContext
from trendstorm.domain.invites.models import Invite
from trendstorm.domain.memberships.models import Role
from trendstorm.infrastructure.email.dev_provider import DevEmailProvider
from trendstorm.infrastructure.email.email_provider import EmailProvider
from trendstorm.infrastructure.email.postmark_provider import PostmarkProvider
from trendstorm.infrastructure.mongo.client import MongoClient
from trendstorm.infrastructure.mongo.repositories.invite_repository import MongoInviteRepository
from trendstorm.infrastructure.mongo.repositories.membership_repository import (
    MongoMembershipRepository,
)
from trendstorm.infrastructure.mongo.repositories.organization_repository import (
    MongoOrganizationRepository,
)
from trendstorm.infrastructure.mongo.repositories.session_repository import (
    MongoRefreshSessionRepository,
)
from trendstorm.infrastructure.mongo.repositories.user_repository import MongoUserRepository
from trendstorm.services.auth.invitation_service import InvitationService
from trendstorm.services.auth.session_service import SessionService
from trendstorm.shared.config import get_settings
from trendstorm.shared.errors import AuthorizationError, NotFoundError
from trendstorm.shared.logging import get_logger
from trendstorm.utils.headers_docs import require_tenant

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/invites", tags=["invites"])


class InviteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    tenant_id: str
    email: str
    roles: list[str]
    status: str
    expires_at: datetime
    created_at: datetime


class CreateInviteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    roles: list[Role] = Field(default_factory=lambda: [Role.MEMBER])


def _to_response(invite: Invite) -> InviteResponse:
    return InviteResponse(
        id=invite.id,
        tenant_id=invite.tenant_id,
        email=invite.email,
        roles=[r.value for r in invite.roles],
        status=invite.status.value,
        expires_at=invite.expires_at,
        created_at=invite.created_at,
    )


def _build_invite_svc(mongo: MongoClient, redis: object = None) -> InvitationService:
    settings = get_settings()
    email_provider: EmailProvider = (
        PostmarkProvider(settings.email)
        if settings.email.provider == "postmark"
        else DevEmailProvider(settings.email.from_email)
    )
    return InvitationService(
        invite_repo=MongoInviteRepository(mongo),
        membership_repo=MongoMembershipRepository(mongo),
        email_provider=email_provider,
        email_settings=settings.email,
        mongo=mongo,
    )


def _auth_context(request: Request) -> AuthContext:
    ctx = getattr(request.state, "auth_context", None)
    if ctx is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated.")
    return ctx  # type: ignore[no-any-return]


# Tenant-scoped endpoints (require X-Tenant-ID).
_tenant_router = APIRouter(dependencies=[Depends(require_tenant)])


@_tenant_router.post("", status_code=status.HTTP_201_CREATED)
async def create_invite(
    body: CreateInviteRequest, request: Request, mongo: MongoDep
) -> InviteResponse:
    ctx = _auth_context(request)
    if ctx.user_id is None:
        raise AuthorizationError("API key auth cannot send invites.")
    membership_repo = MongoMembershipRepository(mongo)
    caller_membership = await membership_repo.get_for_user(ctx.tenant_id, ctx.user_id)
    if caller_membership is None or not caller_membership.is_admin_or_above:
        raise AuthorizationError("Admin role required to send invites.")
    user_repo = MongoUserRepository(mongo)
    inviter = await user_repo.get(ctx.user_id)
    if inviter is None:
        raise NotFoundError("Inviter user not found.")
    org_repo = MongoOrganizationRepository(mongo)
    org = await org_repo.get(ctx.tenant_id)
    org_name = org.name if org else ctx.tenant_id

    svc = _build_invite_svc(mongo)
    invite, _ = await svc.invite_user(
        tenant_id=ctx.tenant_id,
        email=str(body.email),
        roles=body.roles,
        invited_by=inviter,
        org_name=org_name,
    )
    return _to_response(invite)


@_tenant_router.get("")
async def list_invites(request: Request, mongo: MongoDep) -> list[InviteResponse]:
    ctx = _auth_context(request)
    invite_repo = MongoInviteRepository(mongo)
    invites = await invite_repo.list_pending_for_tenant(ctx.tenant_id)
    return [_to_response(i) for i in invites]


@_tenant_router.delete("/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invite(invite_id: str, request: Request, mongo: MongoDep) -> None:
    ctx = _auth_context(request)
    if ctx.user_id is None:
        raise AuthorizationError("API key auth cannot revoke invites.")
    membership_repo = MongoMembershipRepository(mongo)
    caller = await membership_repo.get_for_user(ctx.tenant_id, ctx.user_id)
    if caller is None or not caller.is_admin_or_above:
        raise AuthorizationError("Admin role required.")
    svc = _build_invite_svc(mongo)
    await svc.revoke_invite(ctx.tenant_id, invite_id)


@_tenant_router.post("/{invite_id}/resend")
async def resend_invite(invite_id: str, request: Request, mongo: MongoDep) -> InviteResponse:
    ctx = _auth_context(request)
    if ctx.user_id is None:
        raise AuthorizationError("API key auth cannot resend invites.")
    membership_repo = MongoMembershipRepository(mongo)
    caller = await membership_repo.get_for_user(ctx.tenant_id, ctx.user_id)
    if caller is None or not caller.is_admin_or_above:
        raise AuthorizationError("Admin role required.")
    user_repo = MongoUserRepository(mongo)
    inviter = await user_repo.get(ctx.user_id)
    if inviter is None:
        raise NotFoundError("Inviter not found.")
    org_repo = MongoOrganizationRepository(mongo)
    org = await org_repo.get(ctx.tenant_id)
    org_name = org.name if org else ctx.tenant_id

    svc = _build_invite_svc(mongo)
    new_invite, _ = await svc.resend_invite(
        ctx.tenant_id, invite_id, inviter=inviter, org_name=org_name
    )
    return _to_response(new_invite)


router.include_router(_tenant_router)


# Public endpoints (no tenant header required).
@router.get("/by-token/{token}")
async def preview_invite(token: str, mongo: MongoDep) -> InviteResponse:
    svc = _build_invite_svc(mongo)
    invite = await svc.preview_invite(token)
    return _to_response(invite)


@router.post("/by-token/{token}/accept", status_code=status.HTTP_201_CREATED)
async def accept_invite(
    token: str,
    request: Request,
    mongo: MongoDep,
    redis: RedisDep,
    ts_refresh: Annotated[str | None, Cookie()] = None,
) -> dict[str, Any]:
    """Accept an invite for an already-authenticated user.

    New users should use POST /v1/auth/signup with invite_token instead.
    """
    if not ts_refresh:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated.")
    settings = get_settings()
    user_repo = MongoUserRepository(mongo)
    membership_repo = MongoMembershipRepository(mongo)
    session_repo = MongoRefreshSessionRepository(mongo)
    session_svc = SessionService(
        session_repo=session_repo,
        user_repo=user_repo,
        membership_repo=membership_repo,
        redis=redis,
        jwt_settings=settings.jwt,
    )
    access, _ = await session_svc.refresh_session(ts_refresh)
    claims = session_svc.verify_access_jwt(access)
    user = await user_repo.get(claims["sub"])
    if user is None:
        raise NotFoundError("User not found.")

    svc = _build_invite_svc(mongo)
    membership = await svc.accept_existing_user(token, user)
    return {"tenant_id": membership.tenant_id, "roles": [r.value for r in membership.roles]}
