"""EmailVerificationService — request and consume email verification tokens.

Rate limit: 3 resend requests per hour per user (Redis counter).
Token window: 24 hours.
After consumption: sets email_verified=True on both User and the IdP.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from opentelemetry import trace

from trendstorm.domain.email_verifications.models import EmailVerification
from trendstorm.services.auth.token_utils import generate_token, hash_token
from trendstorm.shared.errors import (
    NotFoundError,
    RateLimitError,
    TokenExpiredError,
    TokenUsedError,
)
from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    from trendstorm.domain.email_verifications.repository import EmailVerificationRepository
    from trendstorm.domain.users.models import User
    from trendstorm.domain.users.repository import UserRepository
    from trendstorm.infrastructure.auth.identity_provider import IdentityProvider
    from trendstorm.infrastructure.email.email_provider import EmailProvider
    from trendstorm.infrastructure.redis.client import RedisClient
    from trendstorm.shared.config import EmailSettings

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)

_EXPIRY_HOURS = 24
_RATE_LIMIT_KEY = "email_verify:user:{user_id}"
_RATE_LIMIT_MAX = 3
_RATE_LIMIT_WINDOW_SECONDS = 3600


class EmailVerificationService:
    def __init__(
        self,
        *,
        verification_repo: EmailVerificationRepository,
        user_repo: UserRepository,
        identity_provider: IdentityProvider,
        email_provider: EmailProvider,
        redis: RedisClient,
        email_settings: EmailSettings,
    ) -> None:
        self._verifications = verification_repo
        self._users = user_repo
        self._idp = identity_provider
        self._email = email_provider
        self._redis = redis
        self._email_cfg = email_settings

    async def request_verification(self, user: User) -> None:
        """Send a new verification email. Rate-limited."""
        with tracer.start_as_current_span("email_verification.request"):
            if user.email_verified:
                return

            await self._check_rate_limit(user.id)
            await self._verifications.delete_pending_for_user(user.id)

            plaintext = generate_token()
            token_hash = hash_token(plaintext)
            now = datetime.now(UTC)
            verification = EmailVerification(
                user_id=user.id,
                email=user.email,
                token_hash=token_hash,
                expires_at=now + timedelta(hours=_EXPIRY_HOURS),
            )
            await self._verifications.insert(verification)

            verify_url = f"{self._email_cfg.app_base_url}/auth/verify?token={plaintext}"
            try:
                await self._email.send_templated(
                    to=user.email,
                    template="verify",
                    variables={
                        "full_name": user.full_name or user.email,
                        "verify_url": verify_url,
                    },
                )
            except Exception as exc:
                logger.warning("email_verification.send_failed", user_id=user.id, error=str(exc))

            logger.info("email_verification.sent", user_id=user.id)

    async def consume_verification(self, token: str) -> User:
        """Validate the token and mark the user's email as verified."""
        with tracer.start_as_current_span("email_verification.consume"):
            token_hash = hash_token(token)
            verification = await self._verifications.get_by_token_hash(token_hash)
            if verification is None:
                raise NotFoundError("Verification token not found.", code="invalid_verification_token")
            if verification.consumed_at is not None:
                raise TokenUsedError()
            if datetime.now(UTC) > verification.expires_at:
                raise TokenExpiredError()

            user = await self._users.get(verification.user_id)
            if user is None:
                raise NotFoundError("User not found.", code="user_not_found")

            await self._verifications.consume(verification.id)
            updated = await self._users.set_email_verified(user.id)
            if user.identity_provider_subject:
                try:
                    await self._idp.mark_email_verified(user.identity_provider_subject)
                except Exception as exc:
                    logger.warning("email_verification.idp_mark_failed", user_id=user.id, error=str(exc))

            logger.info("email_verification.consumed", user_id=user.id)
            return updated or user

    async def _check_rate_limit(self, user_id: str) -> None:
        key = _RATE_LIMIT_KEY.format(user_id=user_id)
        count = await self._redis.client.incr(key)
        if count == 1:
            await self._redis.client.expire(key, _RATE_LIMIT_WINDOW_SECONDS)
        if count > _RATE_LIMIT_MAX:
            raise RateLimitError(
                "Too many verification emails requested.",
                context={"retry_after_seconds": _RATE_LIMIT_WINDOW_SECONDS},
            )
