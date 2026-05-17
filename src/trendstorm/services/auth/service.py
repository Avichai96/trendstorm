"""AuthService — authenticate, provision, and rotate API keys.

AuthService is the single entry point for all auth decisions in the API layer.
The middleware delegates here; it never touches Mongo or JWT directly.

Key operations:
- authenticate_by_key(raw_key) → AuthContext
  Hash the key, look up in Mongo, verify not revoked, update last_used_at.
- authenticate_by_jwt(token) → AuthContext
  Validate JWT signature + claims via JWTValidator, extract tenant_id.
- create_key(tenant_id, name) → (ApiKey, raw_key)
  Generate + hash a new key, persist the ApiKey, return the plaintext ONCE.
- revoke_key(tenant_id, key_id)
  Stamp revoked_at; subsequent lookups return is_active=False.
- rotate_key(tenant_id, key_id) → (ApiKey, raw_key)
  Revoke the old key and create a new one in a single operation. The old
  key stops working immediately.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from opentelemetry import trace

from trendstorm.domain.auth.models import ApiKey, AuthContext
from trendstorm.infrastructure.auth.api_key import (
    generate_api_key,
    hash_key,
    key_prefix,
)
from trendstorm.shared.errors import NotFoundError, TrendStormError
from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    from trendstorm.domain.auth.repository import ApiKeyRepository, TenantRepository
    from trendstorm.infrastructure.auth.jwt import JWTValidator
    from trendstorm.shared.config import AuthSettings

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)


class AuthError(TrendStormError):
    """Invalid credentials or revoked key."""


class AuthService:
    """Service layer for authentication use cases."""

    def __init__(
        self,
        *,
        api_key_repo: ApiKeyRepository,
        tenant_repo: TenantRepository,
        jwt_validator: JWTValidator | None = None,
        settings: AuthSettings,
    ) -> None:
        self._keys = api_key_repo
        self._tenants = tenant_repo
        self._jwt = jwt_validator
        self._settings = settings

    # ------------------------------------------------------------------ #
    # Authentication                                                       #
    # ------------------------------------------------------------------ #

    async def authenticate_by_key(self, raw_key: str) -> AuthContext:
        """Verify a raw API key; return AuthContext on success."""
        with tracer.start_as_current_span("auth.authenticate_by_key"):
            key_hash_value = hash_key(raw_key)

            api_key = await self._keys.get_by_hash(key_hash_value)
            if api_key is None:
                raise AuthError("Invalid API key", code="invalid_api_key")
            if api_key.is_revoked:
                raise AuthError("API key has been revoked", code="revoked_api_key")

            # Constant-time comparison already done by hash lookup.
            # update_last_used is best-effort — never fails the request.
            _task = asyncio.ensure_future(self._keys.update_last_used(api_key.id))  # noqa: RUF006  # fire-and-forget; never blocks authentication

            return AuthContext(
                tenant_id=api_key.tenant_id,
                key_id=api_key.id,
                source="api_key",
            )

    async def authenticate_by_jwt(self, token: str) -> AuthContext:
        """Validate a JWT; return AuthContext on success."""
        if self._jwt is None:
            raise AuthError("JWT auth not configured", code="jwt_not_configured")
        with tracer.start_as_current_span("auth.authenticate_by_jwt"):
            payload = await self._jwt.validate(token)
            tenant_id = self._jwt.extract_tenant_id(payload)
            return AuthContext(
                tenant_id=tenant_id,
                subject=payload.get("sub"),
                source="jwt",
            )

    # ------------------------------------------------------------------ #
    # Key management                                                       #
    # ------------------------------------------------------------------ #

    async def create_key(self, tenant_id: str, name: str) -> tuple[ApiKey, str]:
        """Create a new API key. Returns (persisted ApiKey, plaintext_key).

        The plaintext key is returned ONCE and cannot be recovered later.
        Callers must show it to the user immediately.
        """
        raw = generate_api_key(self._settings.key_env)
        api_key = ApiKey(
            tenant_id=tenant_id,
            name=name,
            key_hash=hash_key(raw),
            key_prefix=key_prefix(raw),
        )
        await self._keys.insert(api_key)
        logger.info("auth.key_created", tenant_id=tenant_id, key_id=api_key.id)
        return api_key, raw

    async def revoke_key(self, tenant_id: str, key_id: str) -> None:
        """Revoke an API key. Subsequent auth attempts with it return 401."""
        existing = await self._keys.get_by_id(tenant_id, key_id)
        if existing is None:
            raise NotFoundError(f"API key {key_id} not found")
        await self._keys.revoke(tenant_id, key_id)
        logger.info("auth.key_revoked", tenant_id=tenant_id, key_id=key_id)

    async def rotate_key(self, tenant_id: str, key_id: str) -> tuple[ApiKey, str]:
        """Revoke old key and issue a new one atomically (from caller's perspective)."""
        await self.revoke_key(tenant_id, key_id)
        existing = await self._keys.get_by_id(tenant_id, key_id)
        name = existing.name if existing else "rotated"
        return await self.create_key(tenant_id, name)

    async def list_keys(self, tenant_id: str) -> list[ApiKey]:
        """Return all keys (active and revoked) for the tenant."""
        return await self._keys.list_for_tenant(tenant_id)
