"""Unit tests for authentication strategies."""
from __future__ import annotations

import pytest

from trendstorm_sdk import ConfigurationError, TrendStormClient
from trendstorm_sdk._auth import ApiKeyAuth, OAuthAuth


@pytest.mark.unit
class TestApiKeyAuth:
    def test_headers_contain_bearer_key(self) -> None:
        auth = ApiKeyAuth("ts_live_abc123")
        headers = auth.headers()
        assert headers["Authorization"] == "Bearer ts_live_abc123"

    def test_headers_are_new_dict_each_call(self) -> None:
        auth = ApiKeyAuth("ts_live_x")
        h1 = auth.headers()
        h2 = auth.headers()
        assert h1 is not h2

    async def test_refresh_is_noop(self) -> None:
        auth = ApiKeyAuth("ts_live_x")
        await auth.refresh()  # should not raise


@pytest.mark.unit
class TestOAuthAuth:
    def test_headers_contain_bearer_token(self) -> None:
        auth = OAuthAuth("my_access_token")
        assert auth.headers()["Authorization"] == "Bearer my_access_token"

    async def test_refresh_noop_when_no_refresh_token(self) -> None:
        auth = OAuthAuth("token", refresh_token=None)
        await auth.refresh()  # no-op, no raise

    async def test_refresh_noop_when_not_expired(self) -> None:
        import time
        auth = OAuthAuth("token", refresh_token="rt", token_url="http://x", expires_at=time.time() + 3600)
        await auth.refresh()  # not expired → no-op


@pytest.mark.unit
class TestClientAuth:
    def test_no_key_raises_configuration_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TRENDSTORM_API_KEY", raising=False)
        monkeypatch.delenv("TRENDSTORM_OAUTH_TOKEN", raising=False)
        with pytest.raises(ConfigurationError):
            TrendStormClient()

    def test_env_var_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRENDSTORM_API_KEY", "ts_test_env")
        monkeypatch.delenv("TRENDSTORM_OAUTH_TOKEN", raising=False)
        ts = TrendStormClient()
        assert isinstance(ts._auth, ApiKeyAuth)

    def test_explicit_api_key_takes_precedence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRENDSTORM_API_KEY", "ts_test_env")
        ts = TrendStormClient(api_key="ts_test_explicit")
        assert ts._auth.headers()["Authorization"] == "Bearer ts_test_explicit"
