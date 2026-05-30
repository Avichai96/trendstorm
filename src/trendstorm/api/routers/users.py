"""Users router — self-service account management.

GET    /v1/users/me                — current user profile
PATCH  /v1/users/me               — update full_name, avatar_url
DELETE /v1/users/me               — schedule account deletion (30-day tombstone)
POST   /v1/users/me/restore       — cancel pending deletion
GET    /v1/users/me/sessions      — list active refresh sessions (security UI)
DELETE /v1/users/me/sessions/{id} — revoke a specific session
GET    /v1/users/me/memberships   — all org memberships for this user
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Cookie, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from trendstorm.api.deps import MongoDep, RedisDep
from trendstorm.domain.sessions.models import RefreshSession
from trendstorm.domain.users.models import User
from trendstorm.infrastructure.auth.auth0_provider import Auth0Provider
from trendstorm.infrastructure.email.dev_provider import DevEmailProvider
from trendstorm.infrastructure.email.email_provider import EmailProvider
from trendstorm.infrastructure.email.postmark_provider import PostmarkProvider
from trendstorm.infrastructure.mongo.client import MongoClient
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
from trendstorm.infrastructure.redis.client import RedisClient
from trendstorm.services.auth.account_deletion_service import AccountDeletionService
from trendstorm.services.auth.session_service import SessionService
from trendstorm.shared.config import get_settings
from trendstorm.shared.errors import NotFoundError
from trendstorm.shared.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/users", tags=["users"])

_REFRESH_COOKIE = "ts_refresh"


class UserResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    email: str
    email_verified: bool
    full_name: str | None
    avatar_url: str | None
    is_active: bool


class UpdateUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    full_name: str | None = Field(default=None, max_length=200)
    avatar_url: str | None = None


class SessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    user_agent: str | None
    ip_address: str | None
    last_used_at: datetime
    expires_at: datetime


class MembershipResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    tenant_id: str
    org_name: str | None
    roles: list[str]
    joined_at: datetime


def _to_user_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        email=user.email,
        email_verified=user.email_verified,
        full_name=user.full_name,
        avatar_url=user.avatar_url,
        is_active=user.is_active,
    )


def _to_session_response(s: RefreshSession) -> SessionResponse:
    return SessionResponse(
        id=s.id,
        user_agent=s.user_agent,
        ip_address=s.ip_address,
        last_used_at=s.last_used_at,
        expires_at=s.expires_at,
    )


async def _resolve_user(ts_refresh: str | None, mongo: MongoClient, redis: RedisClient) -> tuple[User, SessionService]:
    """Resolve the calling user from the refresh cookie."""
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
    return user, session_svc


@router.get("/me")
async def get_me(
    mongo: MongoDep,
    redis: RedisDep,
    ts_refresh: Annotated[str | None, Cookie()] = None,
) -> UserResponse:
    user, _ = await _resolve_user(ts_refresh, mongo, redis)
    return _to_user_response(user)


@router.patch("/me")
async def update_me(
    body: UpdateUserRequest,
    mongo: MongoDep,
    redis: RedisDep,
    ts_refresh: Annotated[str | None, Cookie()] = None,
) -> UserResponse:
    user, _ = await _resolve_user(ts_refresh, mongo, redis)
    user_repo = MongoUserRepository(mongo)
    updated = user.model_copy(update={
        k: v for k, v in {"full_name": body.full_name, "avatar_url": body.avatar_url}.items()
        if v is not None
    })
    await user_repo.update(updated)
    return _to_user_response(updated)


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def delete_me(
    response: Response,
    mongo: MongoDep,
    redis: RedisDep,
    ts_refresh: Annotated[str | None, Cookie()] = None,
) -> None:
    user, session_svc = await _resolve_user(ts_refresh, mongo, redis)
    settings = get_settings()
    email_provider: EmailProvider
    if settings.email.provider == "postmark":
        email_provider = PostmarkProvider(settings.email)
    else:
        email_provider = DevEmailProvider(settings.email.from_email)

    deletion_svc = AccountDeletionService(
        user_repo=MongoUserRepository(mongo),
        membership_repo=MongoMembershipRepository(mongo),
        org_repo=MongoOrganizationRepository(mongo),
        session_service=session_svc,
        identity_provider=Auth0Provider(settings.auth0),
        email_provider=email_provider,
        signup_settings=settings.signup,
        email_settings=settings.email,
    )
    await deletion_svc.schedule_deletion(user)
    response.delete_cookie(_REFRESH_COOKIE)


@router.post("/me/restore", status_code=status.HTTP_204_NO_CONTENT)
async def restore_me(
    mongo: MongoDep,
    redis: RedisDep,
    ts_refresh: Annotated[str | None, Cookie()] = None,
) -> None:
    user, session_svc = await _resolve_user(ts_refresh, mongo, redis)
    settings = get_settings()
    email_provider: EmailProvider
    if settings.email.provider == "postmark":
        email_provider = PostmarkProvider(settings.email)
    else:
        email_provider = DevEmailProvider(settings.email.from_email)
    deletion_svc = AccountDeletionService(
        user_repo=MongoUserRepository(mongo),
        membership_repo=MongoMembershipRepository(mongo),
        org_repo=MongoOrganizationRepository(mongo),
        session_service=session_svc,
        identity_provider=Auth0Provider(settings.auth0),
        email_provider=email_provider,
        signup_settings=settings.signup,
        email_settings=settings.email,
    )
    await deletion_svc.cancel_deletion(user)


@router.get("/me/sessions")
async def list_sessions(
    mongo: MongoDep,
    redis: RedisDep,
    ts_refresh: Annotated[str | None, Cookie()] = None,
) -> list[SessionResponse]:
    user, session_svc = await _resolve_user(ts_refresh, mongo, redis)
    sessions = await session_svc.list_active_sessions(user.id)
    return [_to_session_response(s) for s in sessions]


@router.delete("/me/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(
    session_id: str,
    mongo: MongoDep,
    redis: RedisDep,
    ts_refresh: Annotated[str | None, Cookie()] = None,
) -> None:
    user, session_svc = await _resolve_user(ts_refresh, mongo, redis)
    await session_svc.revoke_session_by_id(user.id, session_id)


@router.get("/me/memberships")
async def list_my_memberships(
    mongo: MongoDep,
    redis: RedisDep,
    ts_refresh: Annotated[str | None, Cookie()] = None,
) -> list[MembershipResponse]:
    user, _ = await _resolve_user(ts_refresh, mongo, redis)
    membership_repo = MongoMembershipRepository(mongo)
    memberships = await membership_repo.list_for_user(user.id)
    org_repo = MongoOrganizationRepository(mongo)

    result = []
    for m in memberships:
        org = await org_repo.get(m.tenant_id)
        result.append(MembershipResponse(
            id=m.id,
            tenant_id=m.tenant_id,
            org_name=org.name if org else None,
            roles=[r.value for r in m.roles],
            joined_at=m.joined_at,
        ))
    return result
