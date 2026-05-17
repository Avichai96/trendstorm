"""Unit tests for infrastructure/llm/registry.py.

Providers are constructed with empty credentials — no real API calls made.
"""
from __future__ import annotations

import pytest

from trendstorm.domain.llm.providers import EmbeddingProvider
from trendstorm.infrastructure.llm.registry import build_embedding_provider
from trendstorm.infrastructure.llm.retry import RetryingEmbeddingProvider
from trendstorm.shared.config import Settings, get_settings
from trendstorm.shared.errors import ConfigError


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    get_settings.cache_clear()
    yield  # type: ignore[misc]
    get_settings.cache_clear()


def _settings(monkeypatch: pytest.MonkeyPatch, provider: str) -> Settings:
    monkeypatch.setenv("LLM__DEFAULT_EMBEDDING_PROVIDER", provider)
    # Non-empty placeholder keys — providers validate at call time, not construction.
    monkeypatch.setenv("GEMINI__API_KEY", "placeholder-gemini-key")
    monkeypatch.setenv("LLM__OPENAI_API_KEY", "placeholder-openai-key")
    return Settings()


@pytest.mark.unit
class TestBuildEmbeddingProvider:
    def test_gemini_provider_selected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = build_embedding_provider(_settings(monkeypatch, "gemini"))
        assert provider.model_id.startswith("gemini.")

    def test_openai_provider_selected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = build_embedding_provider(_settings(monkeypatch, "openai"))
        assert provider.model_id.startswith("openai.")

    def test_ollama_provider_selected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = build_embedding_provider(_settings(monkeypatch, "ollama"))
        assert provider.model_id.startswith("ollama.")

    def test_anthropic_raises_config_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(ConfigError) as exc_info:
            build_embedding_provider(_settings(monkeypatch, "anthropic"))
        assert "anthropic" in str(exc_info.value).lower()

    def test_returned_provider_satisfies_protocol(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = build_embedding_provider(_settings(monkeypatch, "gemini"))
        assert isinstance(provider, EmbeddingProvider)

    def test_provider_is_wrapped_with_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = build_embedding_provider(_settings(monkeypatch, "gemini"))
        assert isinstance(provider, RetryingEmbeddingProvider)

    def test_default_provider_is_gemini(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI__API_KEY", "placeholder-gemini-key")
        provider = build_embedding_provider(Settings())
        assert provider.model_id.startswith("gemini.")

    def test_retry_max_attempts_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM__DEFAULT_EMBEDDING_PROVIDER", "gemini")
        monkeypatch.setenv("LLM__RETRY_MAX_ATTEMPTS", "5")
        monkeypatch.setenv("GEMINI__API_KEY", "placeholder-gemini-key")
        provider = build_embedding_provider(Settings())
        assert isinstance(provider, RetryingEmbeddingProvider)
        assert provider._max_attempts == 5

    def test_retry_base_delay_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM__DEFAULT_EMBEDDING_PROVIDER", "openai")
        monkeypatch.setenv("LLM__RETRY_BASE_DELAY_SECONDS", "2.5")
        monkeypatch.setenv("LLM__OPENAI_API_KEY", "placeholder-openai-key")
        provider = build_embedding_provider(Settings())
        assert isinstance(provider, RetryingEmbeddingProvider)
        assert provider._base_delay == pytest.approx(2.5)

    def test_gemini_model_from_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM__DEFAULT_EMBEDDING_PROVIDER", "gemini")
        monkeypatch.setenv("GEMINI__EMBEDDING_MODEL", "text-embedding-005")
        monkeypatch.setenv("GEMINI__API_KEY", "placeholder-gemini-key")
        provider = build_embedding_provider(Settings())
        assert provider.model_id == "gemini.text-embedding-005"

    def test_openai_model_from_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM__DEFAULT_EMBEDDING_PROVIDER", "openai")
        monkeypatch.setenv("LLM__OPENAI_EMBEDDING_MODEL", "text-embedding-3-large")
        monkeypatch.setenv("LLM__OPENAI_API_KEY", "placeholder-openai-key")
        provider = build_embedding_provider(Settings())
        assert provider.model_id == "openai.text-embedding-3-large"

    def test_ollama_model_from_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM__DEFAULT_EMBEDDING_PROVIDER", "ollama")
        monkeypatch.setenv("LLM__OLLAMA_EMBEDDING_MODEL", "mxbai-embed-large")
        provider = build_embedding_provider(Settings())
        assert provider.model_id == "ollama.mxbai-embed-large"
