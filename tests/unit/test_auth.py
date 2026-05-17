"""Unit tests for auth domain: API key generation, hashing, AuthService, middleware."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from trendstorm.domain.auth.models import ApiKey, AuthContext
from trendstorm.infrastructure.auth.api_key import (
    generate_api_key,
    hash_key,
    key_prefix,
    parse_env,
)
from trendstorm.services.auth.service import AuthError, AuthService

# ---------------------------------------------------------------------------
# API key generation helpers
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestApiKeyGeneration:
    def test_generate_live_key_format(self) -> None:
        key = generate_api_key("live")
        assert key.startswith("ts_live_")
        parts = key.split("_", maxsplit=2)
        assert len(parts) == 3
        assert parts[2] and len(parts[2]) == 32

    def test_generate_test_key_format(self) -> None:
        key = generate_api_key("test")
        assert key.startswith("ts_test_")

    def test_keys_are_unique(self) -> None:
        keys = {generate_api_key() for _ in range(100)}
        assert len(keys) == 100

    def test_hash_is_sha256_hex(self) -> None:
        raw = "ts_live_abc123"
        result = hash_key(raw)
        expected = hashlib.sha256(raw.encode()).hexdigest()
        assert result == expected
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_hash_is_deterministic(self) -> None:
        raw = generate_api_key()
        assert hash_key(raw) == hash_key(raw)

    def test_key_prefix_returns_8_chars(self) -> None:
        raw = "ts_live_ABCDEFGHIJKLMNOPQRSTUVWXYZ01"
        assert key_prefix(raw) == "ABCDEFGH"

    def test_key_prefix_malformed_raises(self) -> None:
        with pytest.raises(ValueError, match="Malformed"):
            key_prefix("no-underscores-at-all")

    def test_parse_env_live(self) -> None:
        assert parse_env("ts_live_abc") == "live"

    def test_parse_env_test(self) -> None:
        assert parse_env("ts_test_abc") == "test"

    def test_parse_env_malformed_raises(self) -> None:
        with pytest.raises(ValueError, match="Malformed"):
            parse_env("notts_blah_abc")


# ---------------------------------------------------------------------------
# AuthService
# ---------------------------------------------------------------------------

def _make_api_key(*, revoked: bool = False) -> ApiKey:
    raw = generate_api_key()
    return ApiKey(
        tenant_id="01TENANT000000000000000001",
        name="test key",
        key_hash=hash_key(raw),
        key_prefix=key_prefix(raw),
        revoked_at=datetime.now(UTC) if revoked else None,
    ), raw


def _make_auth_service(*, api_key_obj: ApiKey | None = None) -> tuple[AuthService, Any, Any]:
    key_repo = MagicMock()
    tenant_repo = MagicMock()

    key_repo.get_by_hash = AsyncMock(return_value=api_key_obj)
    key_repo.insert = AsyncMock()
    key_repo.update_last_used = AsyncMock()
    key_repo.revoke = AsyncMock()
    key_repo.get_by_id = AsyncMock(return_value=api_key_obj)
    key_repo.list_for_tenant = AsyncMock(return_value=[api_key_obj] if api_key_obj else [])

    from trendstorm.shared.config import AuthSettings
    settings = AuthSettings()

    svc = AuthService(
        api_key_repo=key_repo,
        tenant_repo=tenant_repo,
        settings=settings,
    )
    return svc, key_repo, tenant_repo


@pytest.mark.unit
class TestAuthServiceKey:
    @pytest.mark.asyncio
    async def test_authenticate_by_key_success(self) -> None:
        api_key_obj, raw = _make_api_key()
        svc, key_repo, _ = _make_auth_service(api_key_obj=api_key_obj)
        key_repo.get_by_hash = AsyncMock(return_value=api_key_obj)

        ctx = await svc.authenticate_by_key(raw)

        assert isinstance(ctx, AuthContext)
        assert ctx.tenant_id == api_key_obj.tenant_id
        assert ctx.key_id == api_key_obj.id
        assert ctx.source == "api_key"

    @pytest.mark.asyncio
    async def test_authenticate_by_key_not_found_raises(self) -> None:
        svc, key_repo, _ = _make_auth_service()
        key_repo.get_by_hash = AsyncMock(return_value=None)

        with pytest.raises(AuthError, match="Invalid API key"):
            await svc.authenticate_by_key("ts_live_notreal12345678901234567890123")

    @pytest.mark.asyncio
    async def test_authenticate_by_revoked_key_raises(self) -> None:
        api_key_obj, raw = _make_api_key(revoked=True)
        svc, key_repo, _ = _make_auth_service(api_key_obj=api_key_obj)
        key_repo.get_by_hash = AsyncMock(return_value=api_key_obj)

        with pytest.raises(AuthError, match="revoked"):
            await svc.authenticate_by_key(raw)

    @pytest.mark.asyncio
    async def test_create_key_returns_plaintext_once(self) -> None:
        svc, key_repo, _ = _make_auth_service()
        api_key, raw = await svc.create_key("01TENANT000000000000000001", "my key")

        assert raw.startswith("ts_")
        assert api_key.key_hash == hash_key(raw)
        assert api_key.name == "my key"
        key_repo.insert.assert_called_once()

    @pytest.mark.asyncio
    async def test_revoke_key_not_found_raises(self) -> None:
        svc, key_repo, _ = _make_auth_service()
        key_repo.get_by_id = AsyncMock(return_value=None)

        from trendstorm.shared.errors import NotFoundError
        with pytest.raises(NotFoundError):
            await svc.revoke_key("01TENANT000000000000000001", "01KEY0000000000000000001")

    @pytest.mark.asyncio
    async def test_list_keys_returns_all(self) -> None:
        api_key_obj, _ = _make_api_key()
        svc, _, _ = _make_auth_service(api_key_obj=api_key_obj)

        keys = await svc.list_keys("01TENANT000000000000000001")
        assert len(keys) == 1
        assert keys[0] is api_key_obj


@pytest.mark.unit
class TestAuthServiceJWT:
    @pytest.mark.asyncio
    async def test_jwt_not_configured_raises(self) -> None:
        svc, _, _ = _make_auth_service()
        # No jwt_validator configured

        with pytest.raises(AuthError, match="not configured"):
            await svc.authenticate_by_jwt("some.jwt.token")

    @pytest.mark.asyncio
    async def test_jwt_valid_returns_context(self) -> None:
        mock_jwt = MagicMock()
        mock_jwt.validate = AsyncMock(return_value={"sub": "user-123", "https://trendstorm.ai/tenant_id": "01TENANT000000000000000001"})
        mock_jwt.extract_tenant_id = MagicMock(return_value="01TENANT000000000000000001")

        svc, _, _ = _make_auth_service()
        svc._jwt = mock_jwt

        ctx = await svc.authenticate_by_jwt("valid.token")
        assert ctx.tenant_id == "01TENANT000000000000000001"
        assert ctx.subject == "user-123"
        assert ctx.source == "jwt"


# ---------------------------------------------------------------------------
# ApiKey model properties
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestApiKeyModel:
    def test_is_revoked_false_when_no_revoked_at(self) -> None:
        key, _ = _make_api_key()
        assert not key.is_revoked
        assert key.is_active

    def test_is_revoked_true_when_revoked_at_set(self) -> None:
        key, _ = _make_api_key(revoked=True)
        assert key.is_revoked
        assert not key.is_active
