"""PasswordResetService — request and consume password resets.

We own the token lifecycle (not Auth0). When the user clicks the email link,
we validate our token and call IdentityProvider.set_password() on Auth0.

Security properties:
- Rate limit: 5 requests/hour per email, 10/hour per IP (Redis token bucket).
- Don't reveal whether the email exists (always return 200 on request).
- Token is single-use (consumed_at guards replay).
- After successful reset: revoke all active sessions (defensive).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from opentelemetry import trace

from trendstorm.domain.password_resets.models import PasswordReset
from trendstorm.services.auth.token_utils import generate_token, hash_token
from trendstorm.shared.errors import (
    NotFoundError,
    RateLimitError,
    TokenExpiredError,
    TokenUsedError,
)
from trendstorm.shared.logging import get_logger
from trendstorm.shared.metrics.registry import METRICS

if TYPE_CHECKING:
    from trendstorm.domain.password_resets.repository import PasswordResetRepository
    from trendstorm.domain.users.repository import UserRepository
    from trendstorm.infrastructure.auth.identity_provider import IdentityProvider
    from trendstorm.infrastructure.email.email_provider import EmailProvider
    from trendstorm.infrastructure.redis.client import RedisClient
    from trendstorm.services.auth.session_service import SessionService
    from trendstorm.shared.config import EmailSettings

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)

_EXPIRY_MINUTES = 60
_RATE_LIMIT_EMAIL_KEY = "pw_reset:email:{email}"
_RATE_LIMIT_IP_KEY = "pw_reset:ip:{ip}"
_RATE_LIMIT_EMAIL_MAX = 5
_RATE_LIMIT_IP_MAX = 10
_RATE_LIMIT_WINDOW_SECONDS = 3600


class PasswordResetService:
    def __init__(
        self,
        *,
        reset_repo: PasswordResetRepository,
        user_repo: UserRepository,
        identity_provider: IdentityProvider,
        email_provider: EmailProvider,
        session_service: SessionService,
        redis: RedisClient,
        email_settings: EmailSettings,
    ) -> None:
        self._resets = reset_repo
        self._users = user_repo
        self._idp = identity_provider
        self._email = email_provider
        self._sessions = session_service
        self._redis = redis
        self._email_cfg = email_settings

    async def request_reset(self, email: str, ip: str) -> None:
        """Request a password reset. Never reveals whether the email exists."""
        with tracer.start_as_current_span("password_reset.request"):
            email = email.lower().strip()
            await self._check_rate_limits(email, ip)

            user = await self._users.get_by_email(email)
            if user is None:
                # Intentionally silent — don't reveal email existence.
                logger.info("password_reset.email_not_found_silent", email=email)
                return
            if not user.is_active:
                return

            plaintext = generate_token()
            token_hash = hash_token(plaintext)
            now = datetime.now(UTC)
            reset = PasswordReset(
                user_id=user.id,
                token_hash=token_hash,
                expires_at=now + timedelta(minutes=_EXPIRY_MINUTES),
                requested_from_ip=ip,
            )
            # Invalidate any existing pending resets for this user.
            await self._resets.delete_pending_for_user(user.id)
            await self._resets.insert(reset)

            reset_url = f"{self._email_cfg.app_base_url}/auth/reset?token={plaintext}"
            try:
                await self._email.send_templated(
                    to=email,
                    template="reset",
                    variables={
                        "full_name": user.full_name or email,
                        "reset_url": reset_url,
                    },
                )
            except Exception as exc:
                logger.warning("password_reset.email_failed", user_id=user.id, error=str(exc))

            logger.info("password_reset.requested", user_id=user.id)
            try:
                METRICS.password_resets_requested.labels(status="sent").inc()
            except Exception:
                pass

    async def consume_reset(self, token: str, new_password: str) -> None:
        """Validate the token, update the password, revoke all sessions."""
        with tracer.start_as_current_span("password_reset.consume"):
            token_hash = hash_token(token)
            reset = await self._resets.get_by_token_hash(token_hash)
            if reset is None:
                raise NotFoundError("Reset token not found.", code="invalid_reset_token")
            if reset.consumed_at is not None:
                raise TokenUsedError()
            if datetime.now(UTC) > reset.expires_at:
                raise TokenExpiredError()

            user = await self._users.get(reset.user_id)
            if user is None or not user.is_active:
                raise NotFoundError("User not found.", code="user_not_found")
            if user.identity_provider_subject is None:
                raise NotFoundError("No IdP account linked.", code="no_idp_subject")

            await self._resets.consume(reset.id)
            await self._idp.set_password(user.identity_provider_subject, new_password)
            await self._sessions.revoke_all_for_user(user.id)
            logger.info("password_reset.consumed", user_id=user.id)

    async def _check_rate_limits(self, email: str, ip: str) -> None:
        email_key = _RATE_LIMIT_EMAIL_KEY.format(email=email)
        ip_key = _RATE_LIMIT_IP_KEY.format(ip=ip)
        pipe = self._redis.client.pipeline()
        pipe.incr(email_key)
        pipe.expire(email_key, _RATE_LIMIT_WINDOW_SECONDS)
        pipe.incr(ip_key)
        pipe.expire(ip_key, _RATE_LIMIT_WINDOW_SECONDS)
        results = await pipe.execute()
        email_count, _, ip_count, _ = results
        if email_count > _RATE_LIMIT_EMAIL_MAX:
            try:
                METRICS.password_resets_requested.labels(status="rate_limited").inc()
            except Exception:
                pass
            raise RateLimitError(
                "Too many password reset requests for this email.",
                context={"retry_after_seconds": _RATE_LIMIT_WINDOW_SECONDS},
            )
        if ip_count > _RATE_LIMIT_IP_MAX:
            try:
                METRICS.password_resets_requested.labels(status="rate_limited").inc()
            except Exception:
                pass
            raise RateLimitError(
                "Too many password reset requests from this IP.",
                context={"retry_after_seconds": _RATE_LIMIT_WINDOW_SECONDS},
            )
