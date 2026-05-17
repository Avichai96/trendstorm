"""Unit tests for the Settings module."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from trendstorm.shared.config import (
    AnalysisSettings,
    Environment,
    GeminiSettings,
    LLMProvider,
    LogFormat,
    Settings,
    SSESettings,
    get_settings,
)


@pytest.mark.unit
class TestSettings:
    """Settings should load from env vars with nested-delimiter mapping."""

    def test_defaults_when_no_env(self) -> None:
        s = Settings()
        assert s.app.name == "trendstorm"
        assert s.app.env == Environment.LOCAL
        assert s.api.port == 8000
        assert s.mongo.max_pool_size == 100

    def test_nested_env_loading(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APP__NAME", "test-app")
        monkeypatch.setenv("APP__LOG_FORMAT", "console")
        monkeypatch.setenv("API__PORT", "9999")
        monkeypatch.setenv("MONGO__MAX_POOL_SIZE", "50")

        s = Settings()
        assert s.app.name == "test-app"
        assert s.app.log_format == LogFormat.CONSOLE
        assert s.api.port == 9999
        assert s.mongo.max_pool_size == 50

    def test_csv_cors_origins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("API__CORS_ORIGINS", "http://a.com,http://b.com")
        s = Settings()
        assert s.api.cors_origins == ["http://a.com", "http://b.com"]

    def test_secret_str_hides_value_in_repr(self) -> None:
        s = Settings()
        s_repr = repr(s)
        # The mongo URI contains 'rootpass'; SecretStr must hide it.
        assert "rootpass" not in s_repr

    def test_invalid_port_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("API__PORT", "99999999")
        with pytest.raises(ValueError):
            Settings()

    def test_invalid_similarity_threshold(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REDIS__SEMANTIC_CACHE_SIMILARITY_THRESHOLD", "1.5")
        with pytest.raises(ValueError):
            Settings()

    def test_settings_are_frozen(self) -> None:
        s = Settings()
        with pytest.raises(ValidationError):
            s.app.name = "other"  # type: ignore[misc]

    def test_get_settings_is_cached(self) -> None:
        get_settings.cache_clear()
        a = get_settings()
        b = get_settings()
        assert a is b  # same instance


@pytest.mark.unit
class TestIngestSettings:
    def test_defaults(self) -> None:
        s = Settings()
        assert s.ingest.concurrency_per_job == 16
        assert s.ingest.fetch_timeout_seconds == 30
        assert s.ingest.rate_limit_burst == 5
        assert s.ingest.rate_limit_rate == 2.0

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("INGEST__CONCURRENCY_PER_JOB", "8")
        monkeypatch.setenv("INGEST__FETCH_TIMEOUT_SECONDS", "60")
        s = Settings()
        assert s.ingest.concurrency_per_job == 8
        assert s.ingest.fetch_timeout_seconds == 60

    def test_concurrency_bounds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("INGEST__CONCURRENCY_PER_JOB", "200")
        with pytest.raises(ValueError):
            Settings()

    def test_rate_limit_rate_positive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("INGEST__RATE_LIMIT_RATE", "0")
        with pytest.raises(ValueError):
            Settings()


@pytest.mark.unit
class TestGeminiSettings:
    def test_defaults(self) -> None:
        s = Settings()
        assert s.gemini.embedding_model == "text-embedding-004"
        assert s.gemini.chat_model == "gemini-2.0-flash"
        assert s.gemini.api_key.get_secret_value() == ""

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI__API_KEY", "test-key-xyz")
        monkeypatch.setenv("GEMINI__EMBEDDING_MODEL", "text-embedding-005")
        s = Settings()
        assert s.gemini.api_key.get_secret_value() == "test-key-xyz"
        assert s.gemini.embedding_model == "text-embedding-005"

    def test_api_key_hidden_in_repr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI__API_KEY", "super-secret-gemini-key")
        s = Settings()
        assert "super-secret-gemini-key" not in repr(s)

    def test_gemini_settings_are_frozen(self) -> None:
        s = Settings()
        with pytest.raises(ValidationError):
            s.gemini.embedding_model = "other"  # type: ignore[misc]


@pytest.mark.unit
class TestLLMProviderDefaults:
    def test_default_embedding_provider_is_gemini(self) -> None:
        s = Settings()
        assert s.llm.default_embedding_provider == LLMProvider.GEMINI

    def test_default_chat_provider_is_gemini(self) -> None:
        s = Settings()
        assert s.llm.default_chat_provider == LLMProvider.GEMINI

    def test_provider_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM__DEFAULT_EMBEDDING_PROVIDER", "openai")
        monkeypatch.setenv("LLM__DEFAULT_CHAT_PROVIDER", "anthropic")
        s = Settings()
        assert s.llm.default_embedding_provider == LLMProvider.OPENAI
        assert s.llm.default_chat_provider == LLMProvider.ANTHROPIC

    def test_invalid_provider_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM__DEFAULT_EMBEDDING_PROVIDER", "not-a-provider")
        with pytest.raises(ValueError):
            Settings()

    def test_gemini_in_llm_provider_enum(self) -> None:
        assert LLMProvider.GEMINI == "gemini"
        assert LLMProvider("gemini") is LLMProvider.GEMINI

    def test_standalone_gemini_settings_defaults(self) -> None:
        # GeminiSettings can be instantiated on its own; defaults hold.
        # (GEMINI__* prefix is only resolved by the root Settings model.)
        g = GeminiSettings()
        assert g.embedding_model == "text-embedding-004"
        assert g.chat_model == "gemini-2.0-flash"
        assert g.api_key.get_secret_value() == ""


@pytest.mark.unit
class TestAnalysisSettings:
    def test_defaults(self) -> None:
        s = Settings()
        assert s.analysis.retrieval_k == 50
        assert s.analysis.rerank_k == 30
        assert s.analysis.final_k == 10
        assert s.analysis.query_expansion_count == 3
        assert s.analysis.validator_threshold == 0.75
        assert s.analysis.max_refinement_loops == 2

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANALYSIS__RETRIEVAL_K", "100")
        monkeypatch.setenv("ANALYSIS__FINAL_K", "20")
        monkeypatch.setenv("ANALYSIS__VALIDATOR_THRESHOLD", "0.9")
        s = Settings()
        assert s.analysis.retrieval_k == 100
        assert s.analysis.final_k == 20
        assert s.analysis.validator_threshold == 0.9

    def test_threshold_bounds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANALYSIS__VALIDATOR_THRESHOLD", "1.5")
        with pytest.raises(ValueError):
            Settings()

    def test_retrieval_k_bounds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANALYSIS__RETRIEVAL_K", "0")
        with pytest.raises(ValueError):
            Settings()

    def test_standalone_instantiation(self) -> None:
        a = AnalysisSettings()
        assert a.max_refinement_loops == 2


@pytest.mark.unit
class TestSSESettings:
    def test_defaults(self) -> None:
        s = Settings()
        assert s.sse.heartbeat_seconds == 15
        assert s.sse.event_log_ttl_hours == 24
        assert s.sse.channel_prefix == "stream"

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SSE__HEARTBEAT_SECONDS", "30")
        monkeypatch.setenv("SSE__EVENT_LOG_TTL_HOURS", "48")
        monkeypatch.setenv("SSE__CHANNEL_PREFIX", "ts_stream")
        s = Settings()
        assert s.sse.heartbeat_seconds == 30
        assert s.sse.event_log_ttl_hours == 48
        assert s.sse.channel_prefix == "ts_stream"

    def test_heartbeat_bounds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SSE__HEARTBEAT_SECONDS", "0")
        with pytest.raises(ValueError):
            Settings()

    def test_ttl_bounds_low(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SSE__EVENT_LOG_TTL_HOURS", "0")
        with pytest.raises(ValueError):
            Settings()

    def test_ttl_bounds_high(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SSE__EVENT_LOG_TTL_HOURS", "200")
        with pytest.raises(ValueError):
            Settings()

    def test_standalone_instantiation(self) -> None:
        sse = SSESettings()
        assert sse.heartbeat_seconds == 15
        assert sse.event_log_ttl_hours == 24


@pytest.mark.unit
class TestEnvironmentPredicates:
    def test_is_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APP__ENV", "local")
        assert Settings().app.is_local is True

    def test_is_prod(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APP__ENV", "prod")
        assert Settings().app.is_prod is True
        assert Settings().app.is_local is False
