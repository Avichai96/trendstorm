"""SessionService — issue, refresh, and revoke user sessions.

Access token:  HS256 JWT, 15-minute TTL, claims: sub, tenant_id, roles, email.
Refresh token: 32-byte random, SHA-256 hash stored in Redis (primary lookup)
               and Mongo (audit trail / security UI).

Refresh token rotation: on every /auth/refresh call the old Redis key is
deleted and a new token + new Redis key are issued. Mongo last_used_at
is updated. The old Mongo record is stamped revoked_at.

Revocation: delete from Redis (immediate), stamp revoked_at in Mongo.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import jwt as pyjwt

from trendstorm.domain.sessions.models import RefreshSession
from trendstorm.services.auth.token_utils import generate_token, hash_token
from trendstorm.shared.errors import AuthenticationError
from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    from trendstorm.domain.memberships.repository import MembershipRepository
    from trendstorm.domain.sessions.repository import RefreshSessionRepository
    from trendstorm.domain.users.repository import UserRepository
    from trendstorm.infrastructure.redis.client import RedisClient
    from trendstorm.shared.config import JWTSettings

logger = get_logger(__name__)

_RT_KEY_PREFIX = "rt:"  # Redis key: rt:{token_hash}


class SessionService:
    def __init__(
        self,
        *,
        session_repo: RefreshSessionRepository,
        user_repo: UserRepository,
        membership_repo: MembershipRepository,
        redis: RedisClient,
        jwt_settings: JWTSettings,
    ) -> None:
        self._sessions = session_repo
        self._users = user_repo
        self._members = membership_repo
        self._redis = redis
        self._jwt = jwt_settings

    # ------------------------------------------------------------------ #
    # Issue                                                                #
    # ------------------------------------------------------------------ #

    async def issue_session(
        self,
        user_id: str,
        tenant_id: str,
        *,
        user_agent: str | None = None,
        ip: str | None = None,
    ) -> tuple[str, str]:
        """Return (access_jwt, refresh_token_plaintext)."""
        user = await self._users.get(user_id)
        if user is None:
            raise AuthenticationError(f"User {user_id} not found", code="user_not_found")
        membership = await self._members.get_for_user(tenant_id, user_id)
        roles = [r.value for r in membership.roles] if membership else []

        refresh_plain = generate_token()
        refresh_hash = hash_token(refresh_plain)
        now = datetime.now(UTC)
        expires_at = now + timedelta(days=self._jwt.refresh_token_expire_days)

        session = RefreshSession(
            user_id=user_id,
            tenant_id=tenant_id,
            refresh_token_hash=refresh_hash,
            user_agent=user_agent,
            ip_address=ip,
            expires_at=expires_at,
        )
        await self._sessions.insert(session)

        # Redis: rt:{hash} → session_id, TTL aligned to expires_at
        ttl_seconds = int((expires_at - now).total_seconds())
        await self._redis.client.setex(
            f"{_RT_KEY_PREFIX}{refresh_hash}",
            ttl_seconds,
            session.id,
        )

        access_jwt = self._issue_access_jwt(
            user_id=user_id,
            tenant_id=tenant_id,
            email=user.email,
            roles=roles,
        )
        return access_jwt, refresh_plain

    # ------------------------------------------------------------------ #
    # Refresh                                                              #
    # ------------------------------------------------------------------ #

    async def refresh_session(
        self, refresh_token: str, *, user_agent: str | None = None, ip: str | None = None
    ) -> tuple[str, str]:
        """Rotate refresh token; return (new_access_jwt, new_refresh_plaintext)."""
        old_hash = hash_token(refresh_token)
        session_id = await self._redis.client.get(f"{_RT_KEY_PREFIX}{old_hash}")
        if session_id is None:
            raise AuthenticationError("Refresh token not found or expired", code="invalid_token")

        session = await self._sessions.get(session_id.decode() if isinstance(session_id, bytes) else session_id)
        if session is None or not session.is_active:
            await self._redis.client.delete(f"{_RT_KEY_PREFIX}{old_hash}")
            raise AuthenticationError("Session revoked", code="session_revoked")

        # Rotate: revoke old, issue new.
        await self._redis.client.delete(f"{_RT_KEY_PREFIX}{old_hash}")
        await self._sessions.revoke(session.id)

        return await self.issue_session(
            session.user_id,
            session.tenant_id,
            user_agent=user_agent or session.user_agent,
            ip=ip or session.ip_address,
        )

    # ------------------------------------------------------------------ #
    # Revoke                                                               #
    # ------------------------------------------------------------------ #

    async def revoke_session(self, refresh_token: str) -> None:
        """Revoke a single session by plaintext refresh token."""
        token_hash = hash_token(refresh_token)
        session_id = await self._redis.client.get(f"{_RT_KEY_PREFIX}{token_hash}")
        if session_id is not None:
            await self._redis.client.delete(f"{_RT_KEY_PREFIX}{token_hash}")
            sid = session_id.decode() if isinstance(session_id, bytes) else session_id
            await self._sessions.revoke(sid)

    async def revoke_session_by_id(self, user_id: str, session_id: str) -> None:
        """Revoke a session by ID (for the security UI delete flow)."""
        session = await self._sessions.get(session_id)
        if session is None or session.user_id != user_id:
            return
        await self._redis.client.delete(f"{_RT_KEY_PREFIX}{session.refresh_token_hash}")
        await self._sessions.revoke(session_id)

    async def revoke_all_for_user(self, user_id: str) -> None:
        """Revoke all active sessions — called on password reset and account deletion."""
        sessions = await self._sessions.list_active_for_user(user_id)
        for s in sessions:
            await self._redis.client.delete(f"{_RT_KEY_PREFIX}{s.refresh_token_hash}")
        await self._sessions.revoke_all_for_user(user_id)

    async def list_active_sessions(self, user_id: str) -> list[RefreshSession]:
        return await self._sessions.list_active_for_user(user_id)

    async def issue_access_jwt_for_org_switch(self, user_id: str, tenant_id: str) -> str:
        """Issue a fresh access JWT when switching the active organization.

        Loads the user and membership from the repository so the email and
        roles in the JWT are accurate for the target org.
        """
        user = await self._users.get(user_id)
        membership = await self._members.get_for_user(tenant_id, user_id)
        roles = [r.value for r in membership.roles] if membership else []
        email = user.email if user else ""
        return self._issue_access_jwt(
            user_id=user_id, tenant_id=tenant_id, email=email, roles=roles
        )

    # ------------------------------------------------------------------ #
    # JWT helpers                                                          #
    # ------------------------------------------------------------------ #

    def _issue_access_jwt(
        self, *, user_id: str, tenant_id: str, email: str, roles: list[str]
    ) -> str:
        now = datetime.now(UTC)
        payload: dict[str, Any] = {
            "sub": user_id,
            "tenant_id": tenant_id,
            "email": email,
            "roles": roles,
            "iat": int(now.timestamp()),
            "exp": int(
                (now + timedelta(minutes=self._jwt.access_token_expire_minutes)).timestamp()
            ),
        }
        return pyjwt.encode(
            payload,
            self._jwt.secret.get_secret_value(),
            algorithm=self._jwt.algorithm,
        )

    def verify_access_jwt(self, token: str) -> dict[str, Any]:
        """Decode and verify an access JWT. Raises AuthenticationError on failure."""
        try:
            return pyjwt.decode(
                token,
                self._jwt.secret.get_secret_value(),
                algorithms=[self._jwt.algorithm],
            )
        except pyjwt.ExpiredSignatureError:
            raise AuthenticationError("Access token has expired", code="expired_token") from None
        except pyjwt.InvalidTokenError as exc:
            raise AuthenticationError(f"Invalid access token: {exc}", code="invalid_token") from exc
