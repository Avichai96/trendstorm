"""Authentication router — public endpoints, no tenant context required.

All endpoints here are unauthenticated entry points. Rate limiting is applied
per-IP by the RateLimitMiddleware.

POST /v1/auth/signup
POST /v1/auth/login
POST /v1/auth/logout
POST /v1/auth/refresh
POST /v1/auth/verify-email
POST /v1/auth/resend-verification
POST /v1/auth/password-reset-request
POST /v1/auth/password-reset-confirm
GET  /v1/auth/oauth/{provider}/start
POST /v1/auth/oauth/{provider}/callback
"""

from __future__ import annotations

import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Cookie, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from trendstorm.api.deps import MongoDep, RedisDep
from trendstorm.infrastructure.auth.auth0_provider import Auth0Provider
from trendstorm.infrastructure.auth.identity_provider import OAuthProvider
from trendstorm.infrastructure.email.dev_provider import DevEmailProvider
from trendstorm.infrastructure.email.email_provider import EmailProvider
from trendstorm.infrastructure.email.postmark_provider import PostmarkProvider
from trendstorm.infrastructure.mongo.repositories.email_verification_repository import (
    MongoEmailVerificationRepository,
)
from trendstorm.infrastructure.mongo.repositories.invite_repository import MongoInviteRepository
from trendstorm.infrastructure.mongo.repositories.membership_repository import (
    MongoMembershipRepository,
)
from trendstorm.infrastructure.mongo.repositories.organization_repository import (
    MongoOrganizationRepository,
)
from trendstorm.infrastructure.mongo.repositories.password_reset_repository import (
    MongoPasswordResetRepository,
)
from trendstorm.infrastructure.mongo.repositories.session_repository import (
    MongoRefreshSessionRepository,
)
from trendstorm.infrastructure.mongo.repositories.user_repository import MongoUserRepository
from trendstorm.services.auth.email_verification_service import EmailVerificationService
from trendstorm.services.auth.password_reset_service import PasswordResetService
from trendstorm.services.auth.registration_service import RegistrationService
from trendstorm.services.auth.session_service import SessionService
from trendstorm.shared.config import get_settings
from trendstorm.shared.errors import AuthenticationError, RateLimitError
from trendstorm.shared.logging import get_logger
from trendstorm.shared.metrics.registry import METRICS

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/auth", tags=["auth"])

_REFRESH_COOKIE = "ts_refresh"
_REFRESH_COOKIE_MAX_AGE = 30 * 24 * 3600  # 30 days

# Signup rate-limit constants (per IP and per email, rolling 1-hour window).
_SIGNUP_RATE_EMAIL_MAX = 5
_SIGNUP_RATE_IP_MAX = 10
_SIGNUP_RATE_WINDOW_SECONDS = 3600

# OAuth state CSRF protection: state token lives 2 minutes in Redis.
_OAUTH_STATE_TTL_SECONDS = 120


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class SignupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    password: str = Field(min_length=8)
    invite_token: str | None = None


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    access_token: str
    token_type: str = "bearer"  # noqa: S105
    user_id: str
    tenant_id: str


class UserResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    email: str
    email_verified: bool
    full_name: str | None
    avatar_url: str | None


class VerifyEmailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str


class PasswordResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr


class PasswordResetConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str
    new_password: str = Field(min_length=8)


# ---------------------------------------------------------------------------
# DI helpers (build services per-request from app.state)
# ---------------------------------------------------------------------------


def _build_services(mongo: MongoDep, redis: RedisDep) -> dict[str, Any]:
    settings = get_settings()
    user_repo = MongoUserRepository(mongo)
    org_repo = MongoOrganizationRepository(mongo)
    membership_repo = MongoMembershipRepository(mongo)
    invite_repo = MongoInviteRepository(mongo)
    verification_repo = MongoEmailVerificationRepository(mongo)
    reset_repo = MongoPasswordResetRepository(mongo)
    session_repo = MongoRefreshSessionRepository(mongo)

    email_provider: EmailProvider
    if settings.email.provider == "postmark":
        email_provider = PostmarkProvider(settings.email)
    else:
        email_provider = DevEmailProvider(settings.email.from_email)

    idp = Auth0Provider(settings.auth0)

    session_svc = SessionService(
        session_repo=session_repo,
        user_repo=user_repo,
        membership_repo=membership_repo,
        redis=redis,
        jwt_settings=settings.jwt,
    )
    registration_svc = RegistrationService(
        user_repo=user_repo,
        org_repo=org_repo,
        membership_repo=membership_repo,
        invite_repo=invite_repo,
        identity_provider=idp,
        email_provider=email_provider,
        mongo=mongo,
        signup_settings=settings.signup,
        email_settings=settings.email,
    )
    verification_svc = EmailVerificationService(
        verification_repo=verification_repo,
        user_repo=user_repo,
        identity_provider=idp,
        email_provider=email_provider,
        redis=redis,
        email_settings=settings.email,
    )
    reset_svc = PasswordResetService(
        reset_repo=reset_repo,
        user_repo=user_repo,
        identity_provider=idp,
        email_provider=email_provider,
        session_service=session_svc,
        redis=redis,
        email_settings=settings.email,
    )
    return {
        "user_repo": user_repo,
        "idp": idp,
        "session_svc": session_svc,
        "registration_svc": registration_svc,
        "verification_svc": verification_svc,
        "reset_svc": reset_svc,
    }


def _client_ip(request: Request) -> str:
    xff = request.headers.get("X-Forwarded-For")
    return xff.split(",")[0].strip() if xff else (request.client.host if request.client else "unknown")


async def _check_signup_rate_limits(email: str, ip: str, redis: RedisDep) -> None:
    """Rate-limit signup per email and per IP using a Redis pipeline."""
    email_key = f"signup:email:{email}"
    ip_key = f"signup:ip:{ip}"
    pipe = redis.client.pipeline()
    pipe.incr(email_key)
    pipe.expire(email_key, _SIGNUP_RATE_WINDOW_SECONDS)
    pipe.incr(ip_key)
    pipe.expire(ip_key, _SIGNUP_RATE_WINDOW_SECONDS)
    results = await pipe.execute()
    email_count, _, ip_count, _ = results
    if email_count > _SIGNUP_RATE_EMAIL_MAX:
        raise RateLimitError(
            "Too many signup attempts for this email.",
            context={"retry_after_seconds": _SIGNUP_RATE_WINDOW_SECONDS},
        )
    if ip_count > _SIGNUP_RATE_IP_MAX:
        raise RateLimitError(
            "Too many signup attempts from this IP.",
            context={"retry_after_seconds": _SIGNUP_RATE_WINDOW_SECONDS},
        )


def _set_refresh_cookie(response: Response, refresh_token: str) -> None:
    response.set_cookie(
        _REFRESH_COOKIE,
        refresh_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=_REFRESH_COOKIE_MAX_AGE,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/signup", status_code=status.HTTP_201_CREATED)
async def signup(
    body: SignupRequest,
    request: Request,
    response: Response,
    mongo: MongoDep,
    redis: RedisDep,
) -> TokenResponse:
    ip = _client_ip(request)
    await _check_signup_rate_limits(str(body.email).lower(), ip, redis)
    svcs = _build_services(mongo, redis)
    user, org = await svcs["registration_svc"].create_account(
        str(body.email),
        body.password,
        invite_token=body.invite_token,
        ip=ip,
    )
    # Send verification email (non-blocking if it fails).
    try:
        await svcs["verification_svc"].request_verification(user)
    except Exception as exc:
        logger.warning("signup.verification_email_failed", error=str(exc))

    access, refresh = await svcs["session_svc"].issue_session(
        user.id, org.id, user_agent=request.headers.get("User-Agent"), ip=ip
    )
    _set_refresh_cookie(response, refresh)
    return TokenResponse(access_token=access, user_id=user.id, tenant_id=org.id)


@router.post("/login")
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    mongo: MongoDep,
    redis: RedisDep,
) -> TokenResponse:
    svcs = _build_services(mongo, redis)
    ip = _client_ip(request)
    login_status = "failed"
    try:
        external = await svcs["idp"].authenticate(str(body.email), body.password)
        user = await svcs["user_repo"].get_by_subject(external.subject)
        if user is None or not user.is_active:
            raise AuthenticationError("Account not found.", code="account_not_found")
        # Pick first active membership as default tenant.
        membership_repo = MongoMembershipRepository(mongo)
        memberships = await membership_repo.list_for_user(user.id)
        active = [m for m in memberships if m.tenant_id]
        if not active:
            raise AuthenticationError("No organization found for user.", code="no_org")
        tenant_id = active[0].tenant_id
        access, refresh = await svcs["session_svc"].issue_session(
            user.id, tenant_id, user_agent=request.headers.get("User-Agent"), ip=ip
        )
        _set_refresh_cookie(response, refresh)
        login_status = "success"
        return TokenResponse(access_token=access, user_id=user.id, tenant_id=tenant_id)
    finally:
        try:
            METRICS.logins.labels(method="password", status=login_status).inc()
        except Exception:
            pass


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    mongo: MongoDep,
    redis: RedisDep,
    ts_refresh: Annotated[str | None, Cookie()] = None,
) -> None:
    if ts_refresh:
        svcs = _build_services(mongo, redis)
        await svcs["session_svc"].revoke_session(ts_refresh)
    response.delete_cookie(_REFRESH_COOKIE)


@router.post("/refresh")
async def refresh_token(
    request: Request,
    response: Response,
    mongo: MongoDep,
    redis: RedisDep,
    ts_refresh: Annotated[str | None, Cookie()] = None,
) -> TokenResponse:
    if not ts_refresh:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No refresh token.")
    svcs = _build_services(mongo, redis)
    access, new_refresh = await svcs["session_svc"].refresh_session(
        ts_refresh,
        user_agent=request.headers.get("User-Agent"),
        ip=_client_ip(request),
    )
    _set_refresh_cookie(response, new_refresh)
    # Decode JWT to extract user_id + tenant_id for the response.
    claims = svcs["session_svc"].verify_access_jwt(access)
    return TokenResponse(
        access_token=access,
        user_id=claims["sub"],
        tenant_id=claims["tenant_id"],
    )


@router.post("/verify-email", status_code=status.HTTP_204_NO_CONTENT)
async def verify_email(body: VerifyEmailRequest, mongo: MongoDep, redis: RedisDep) -> None:
    svcs = _build_services(mongo, redis)
    await svcs["verification_svc"].consume_verification(body.token)


@router.post("/resend-verification", status_code=status.HTTP_204_NO_CONTENT)
async def resend_verification(
    request: Request, mongo: MongoDep, redis: RedisDep,
    ts_refresh: Annotated[str | None, Cookie()] = None,
) -> None:
    if not ts_refresh:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated.")
    svcs = _build_services(mongo, redis)
    access, _ = await svcs["session_svc"].refresh_session(ts_refresh)
    claims = svcs["session_svc"].verify_access_jwt(access)
    user = await svcs["user_repo"].get(claims["sub"])
    if user:
        await svcs["verification_svc"].request_verification(user)


@router.post("/password-reset-request", status_code=status.HTTP_204_NO_CONTENT)
async def password_reset_request(
    body: PasswordResetRequest, request: Request, mongo: MongoDep, redis: RedisDep
) -> None:
    svcs = _build_services(mongo, redis)
    await svcs["reset_svc"].request_reset(str(body.email), _client_ip(request))


@router.post("/password-reset-confirm", status_code=status.HTTP_204_NO_CONTENT)
async def password_reset_confirm(
    body: PasswordResetConfirmRequest, mongo: MongoDep, redis: RedisDep
) -> None:
    svcs = _build_services(mongo, redis)
    await svcs["reset_svc"].consume_reset(body.token, body.new_password)


@router.get("/oauth/{provider}/start")
async def oauth_start(
    provider: OAuthProvider, request: Request, mongo: MongoDep, redis: RedisDep
) -> dict[str, str]:
    svcs = _build_services(mongo, redis)
    state = secrets.token_urlsafe(16)
    # Store state for CSRF validation at callback time (2-minute window).
    await redis.client.setex(f"oauth_state:{state}", _OAUTH_STATE_TTL_SECONDS, "1")
    settings = get_settings()
    redirect_uri = f"{settings.email.app_base_url}/auth/oauth/{provider}/callback"
    url = await svcs["idp"].get_oauth_authorize_url(provider, state, redirect_uri)
    return {"url": url, "state": state}


@router.post("/oauth/{provider}/callback")
async def oauth_callback(
    provider: OAuthProvider,
    request: Request,
    response: Response,
    mongo: MongoDep,
    redis: RedisDep,
) -> TokenResponse:
    body = await request.json()
    code = body.get("code", "")
    state = body.get("state", "")
    invite_token = body.get("invite_token")
    # Validate and consume the OAuth state token to prevent CSRF.
    if not state or not await redis.client.get(f"oauth_state:{state}"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired OAuth state parameter.",
        )
    await redis.client.delete(f"oauth_state:{state}")
    svcs = _build_services(mongo, redis)
    settings = get_settings()
    redirect_uri = f"{settings.email.app_base_url}/auth/oauth/{provider}/callback"
    external = await svcs["idp"].exchange_oauth_code(provider, code, state, redirect_uri)

    # Check if user already exists (returning OAuth user).
    user = await svcs["user_repo"].get_by_subject(external.subject)
    if user is None:
        user, org = await svcs["registration_svc"].create_account_from_oauth(
            external, invite_token=invite_token, ip=_client_ip(request)
        )
    else:
        membership_repo = MongoMembershipRepository(mongo)
        memberships = await membership_repo.list_for_user(user.id)
        org_id = memberships[0].tenant_id if memberships else ""

        class _FakeOrg:
            id = org_id
        org = _FakeOrg()

    ip = _client_ip(request)
    access, refresh = await svcs["session_svc"].issue_session(
        user.id, org.id, user_agent=request.headers.get("User-Agent"), ip=ip
    )
    _set_refresh_cookie(response, refresh)
    return TokenResponse(access_token=access, user_id=user.id, tenant_id=org.id)
