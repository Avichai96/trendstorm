"""Application configuration loaded from environment variables.

Design:
    - Nested Pydantic models mirror the .env structure (FOO__BAR__BAZ).
    - All settings are typed and validated at startup.
    - One global `Settings` object accessed via `get_settings()` (cached).
    - Never mutated at runtime — immutable after load.

Pattern:
    The `.env` file uses double-underscore delimiters:
        MONGO__URI=mongodb://...
        MONGO__MAX_POOL_SIZE=100
    Pydantic Settings maps this to:
        settings.mongo.uri
        settings.mongo.max_pool_size

Why immutable, cached settings?
    - Race conditions: if code reads config mid-mutation, behavior is undefined.
    - Testability: tests inject a fresh Settings; production code uses the
      cached singleton.
    - Twelve-factor compliance: config from env, loaded once, never reloaded.
      To change config, restart the process. This is intentional.
"""
from __future__ import annotations

import json
from enum import StrEnum
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

if TYPE_CHECKING:
    from pydantic.fields import FieldInfo


class _CsvFriendlyEnvSource(EnvSettingsSource):
    """Falls back to the raw string when JSON decoding fails.

    This lets field_validators handle non-JSON formats (e.g. CSV for list[str]).
    Without this, pydantic-settings raises before validators ever run.
    """

    def decode_complex_value(self, field_name: str, field_info: FieldInfo, value: Any) -> Any:
        try:
            return super().decode_complex_value(field_name, field_info, value)
        except (ValueError, json.JSONDecodeError):
            return value


# ---------------------------------------------------------------------------
# Enums for closed-set string fields
# ---------------------------------------------------------------------------

class Environment(StrEnum):
    """Deployment environment. Drives sampling, logging, error verbosity."""

    LOCAL = "local"
    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class LogFormat(StrEnum):
    JSON = "json"          # production: machine-parseable
    CONSOLE = "console"    # development: human-friendly with color


class LLMProvider(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    OLLAMA = "ollama"
    GEMINI = "gemini"


# ---------------------------------------------------------------------------
# Nested settings models — one per subsystem
# ---------------------------------------------------------------------------

class AppSettings(BaseSettings):
    """Top-level app metadata."""

    model_config = SettingsConfigDict(frozen=True)

    env: Environment = Environment.LOCAL
    name: str = "trendstorm"
    log_level: LogLevel = LogLevel.INFO
    log_format: LogFormat = LogFormat.JSON

    @property
    def is_local(self) -> bool:
        return self.env == Environment.LOCAL

    @property
    def is_prod(self) -> bool:
        return self.env == Environment.PROD


class MongoSettings(BaseSettings):
    """MongoDB connection settings."""

    uri: SecretStr = Field(
        default=SecretStr(
            "mongodb://root:rootpass@localhost:27017/"
            "?replicaSet=rs0&directConnection=true&authSource=admin"
        ),
        description="Mongo URI including replicaSet and directConnection params.",
    )
    database: str = "trendstorm"
    max_pool_size: int = Field(default=100, ge=1, le=1000)
    min_pool_size: int = Field(default=10, ge=0)
    server_selection_timeout_ms: int = Field(default=5000, ge=100)


class KafkaSettings(BaseSettings):
    """Kafka client settings."""

    bootstrap_servers: str = "localhost:29092"
    client_id: str = "trendstorm"
    consumer_group_prefix: str = "trendstorm"
    security_protocol: Literal["PLAINTEXT", "SASL_PLAINTEXT", "SASL_SSL"] = "PLAINTEXT"
    sasl_mechanism: str | None = None
    sasl_username: str | None = None
    sasl_password: SecretStr | None = None
    # Port for the per-worker Prometheus /metrics HTTP server.
    # Each worker is an isolated container so 9090 is safe as default for all.
    metrics_port: int = Field(default=9090, ge=1, le=65535)

    @property
    def is_secure(self) -> bool:
        return self.security_protocol in {"SASL_PLAINTEXT", "SASL_SSL"}


class RedisSettings(BaseSettings):
    """Redis connection + semantic cache settings."""

    url: SecretStr = Field(default=SecretStr("redis://localhost:6379/0"))
    max_connections: int = Field(default=50, ge=1)
    semantic_cache_ttl_seconds: int = Field(default=3600, ge=0)
    semantic_cache_similarity_threshold: float = Field(default=0.97, ge=0.0, le=1.0)


class VectorSettings(BaseSettings):
    """Vector store settings — abstracted to support Chroma → Pinecone migration."""

    provider: Literal["chroma", "pinecone"] = "chroma"
    chroma_host: str = "localhost"
    chroma_port: int = Field(default=8000, ge=1, le=65535)
    collection_name: str = "trendstorm_chunks"
    embedding_dimensions: int = Field(default=1536, ge=1)


class BlobSettings(BaseSettings):
    """S3-compatible blob storage settings."""

    endpoint: str = "http://localhost:9000"
    access_key: SecretStr = Field(default=SecretStr("minioadmin"))
    secret_key: SecretStr = Field(default=SecretStr("minioadmin"))
    bucket_raw: str = "trendstorm-raw"
    bucket_reports: str = "trendstorm-reports"
    region: str = "us-east-1"


class LLMSettings(BaseSettings):
    """LLM provider settings. SecretStr keys prevent accidental logging."""

    default_provider: LLMProvider = LLMProvider.ANTHROPIC
    routing_strategy: Literal["cost_aware", "quality_first", "local_first"] = "cost_aware"

    # Phase 7+: per-task provider selection (overrides default_provider)
    default_embedding_provider: LLMProvider = LLMProvider.GEMINI
    default_chat_provider: LLMProvider = LLMProvider.GEMINI

    # Retry settings for RetryingEmbeddingProvider (infrastructure/llm/retry.py)
    retry_max_attempts: int = Field(default=3, ge=1, le=10)
    retry_base_delay_seconds: float = Field(default=1.0, gt=0.0)
    retry_max_delay_seconds: float = Field(default=60.0, gt=0.0)

    # Anthropic
    anthropic_api_key: SecretStr = Field(default=SecretStr(""))
    anthropic_model: str = "claude-sonnet-4-5"

    # OpenAI
    openai_api_key: SecretStr = Field(default=SecretStr(""))
    openai_chat_model: str = "gpt-4o-mini"
    openai_embedding_model: str = "text-embedding-3-small"

    # Ollama
    ollama_base_url: str = "http://localhost:11434"
    ollama_default_model: str = "llama3.2:3b"
    ollama_embedding_model: str = "nomic-embed-text"

    # Cohere
    cohere_api_key: SecretStr = Field(default=SecretStr(""))
    cohere_rerank_model: str = "rerank-v3.5"


class OTelSettings(BaseSettings):
    """OpenTelemetry settings."""

    exporter_otlp_endpoint: str = "http://localhost:4317"
    exporter_otlp_protocol: Literal["grpc", "http/protobuf"] = "grpc"
    service_name: str = "trendstorm"
    service_version: str = "0.1.0"
    traces_sampler: str = "parentbased_traceidratio"
    traces_sampler_arg: float = Field(default=1.0, ge=0.0, le=1.0)


class IngestSettings(BaseSettings):
    """Scout / ingestion worker settings — fetcher, rate limiter, concurrency."""

    fetch_timeout_seconds: int = Field(default=30, ge=1)
    max_response_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    user_agent: str = "TrendStormBot/1.0"
    max_redirects: int = Field(default=5, ge=0, le=20)
    rate_limit_rate: float = Field(default=2.0, gt=0.0)
    rate_limit_burst: int = Field(default=5, ge=1)
    concurrency_per_job: int = Field(default=16, ge=1, le=100)


class GeminiSettings(BaseSettings):
    """Google Gemini settings — dev-default embedding + chat provider.

    Free tier: 1500 req/day on gemini-2.0-flash; separate quota for embeddings.
    Env: GEMINI__API_KEY, GEMINI__EMBEDDING_MODEL, GEMINI__CHAT_MODEL.
    """

    model_config = SettingsConfigDict(frozen=True)

    api_key: SecretStr = Field(default=SecretStr(""))
    embedding_model: str = "text-embedding-004"
    chat_model: str = "gemini-2.0-flash"


class AnalysisSettings(BaseSettings):
    """Retrieval funnel widths, validator threshold, and refinement budget.

    Funnel: retrieval_k (per retriever per sub-query)
         → rerank_k (after RRF merge across all sub-queries)
         → final_k  (after cross-encoder reranking)
    """

    # Retrieval funnel
    retrieval_k: int = Field(default=50, ge=1, le=500)
    rerank_k: int = Field(default=30, ge=1, le=200)
    final_k: int = Field(default=10, ge=1, le=100)

    # Query expansion
    query_expansion_count: int = Field(default=3, ge=1, le=5)

    # Validator
    validator_threshold: float = Field(default=0.75, ge=0.0, le=1.0)

    # Refinement budget — must match agents/stages.py MAX_REFINEMENT_LOOPS
    max_refinement_loops: int = Field(default=2, ge=0, le=5)


class EvalThresholds(BaseSettings):
    """Per-dimension pass/fail thresholds for the evaluation panel.

    Scores are in [0, 1]. An EvaluationResult whose dimension score falls
    below the corresponding threshold is flagged for golden curation review.
    """

    faithfulness: float = Field(default=0.85, ge=0.0, le=1.0)
    citation_accuracy: float = Field(default=0.95, ge=0.0, le=1.0)
    relevance: float = Field(default=0.80, ge=0.0, le=1.0)
    coverage: float = Field(default=0.70, ge=0.0, le=1.0)


class EvalSettings(BaseSettings):
    """Evaluation pipeline settings — panel judges, thresholds, sampling, LangSmith.

    production_sample_rate: fraction of successful analyst passes sent to
        eval.sample.v1. hash(job_id) % 100 == 0 gives deterministic ~1% sampling.
    panel_judges: ordered list of model IDs forming the judge panel. Cheap,
        diverse-provider models are preferred — diversity beats intelligence for
        rubric judging and avoids same-model bias.
    langsmith_project_*: separate LangSmith projects for different evaluation
        contexts so dev noise does not pollute eval baselines.
    """

    production_sample_rate: float = Field(default=0.01, ge=0.0, le=1.0)
    panel_judges: list[str] = Field(
        default_factory=lambda: ["gpt-4.1-nano", "gemini-2.5-flash", "claude-haiku-4-5"]
    )
    min_quorum: int = Field(default=2, ge=1, le=10)
    thresholds: EvalThresholds = Field(default_factory=EvalThresholds)
    langsmith_project_dev: str = "trendstorm-dev"
    langsmith_project_eval: str = "trendstorm-eval"
    langsmith_project_prod: str = "trendstorm-prod"

    @field_validator("panel_judges", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        """Allow comma-separated env var: EVAL__PANEL_JUDGES=m1,m2,m3."""
        if isinstance(v, str):
            return [m.strip() for m in v.split(",") if m.strip()]
        return v


class LangSmithSettings(BaseSettings):
    """LangSmith tracing settings (optional)."""

    api_key: SecretStr = Field(default=SecretStr(""))
    project: str = "trendstorm-local"
    tracing: bool = False


class APISettings(BaseSettings):
    """HTTP API server settings."""

    host: str = "0.0.0.0"  # noqa: S104  bind all interfaces — containerized
    port: int = Field(default=8080, ge=1, le=65535)
    workers: int = Field(default=1, ge=1)
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://localhost:5173"]
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        """Allow comma-separated env var: API__CORS_ORIGINS=a,b,c."""
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v


class AuthMode(StrEnum):
    """Controls how incoming requests are authenticated.

    disabled     — no auth (dev-only; production startup refuses this).
    header       — legacy X-Tenant-ID header accepted (deprecation mode).
    key          — API key required (Bearer ts_live_... or ts_test_...).
    oauth        — JWT required (Auth0 or configured IdP).
    key_or_oauth — either API key or JWT works (production default).
    """

    DISABLED    = "disabled"
    HEADER      = "header"       # legacy; logs deprecation warning
    KEY         = "key"
    OAUTH       = "oauth"
    KEY_OR_OAUTH = "key_or_oauth"


class AuthSettings(BaseSettings):
    """Authentication configuration."""

    model_config = SettingsConfigDict(frozen=True)

    mode: AuthMode = AuthMode.HEADER

    # JWT IdP configuration (required when mode includes oauth)
    jwt_issuer_url: str = ""
    jwt_audience: str = ""

    # API key environment tag (drives key prefix in generated keys)
    key_env: Literal["live", "test"] = "live"

    # Per-tenant request rate limit (applied by RateLimitMiddleware)
    rate_limit_requests_per_minute: int = Field(default=100, ge=1)
    rate_limit_burst: int = Field(default=20, ge=1)


class SSESettings(BaseSettings):
    """Server-Sent Events settings — event log durability and live fanout.

    heartbeat_seconds: SSE comment interval to keep idle connections alive.
    event_log_ttl_hours: Redis Stream TTL for per-job event replay history.
    channel_prefix: Redis Pub/Sub channel prefix; channel = {prefix}:{job_id}:live.
    """

    heartbeat_seconds: int = Field(default=15, ge=1, le=300)
    event_log_ttl_hours: int = Field(default=24, ge=1, le=168)
    channel_prefix: str = "stream"


# ---------------------------------------------------------------------------
# Root settings — composes all subsystems
# ---------------------------------------------------------------------------

class Settings(BaseSettings):
    """Root settings object.

    All subsystem settings are accessible as nested attributes:
        settings = get_settings()
        settings.mongo.uri.get_secret_value()
        settings.app.is_prod
    """

    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),   # .env.local overrides .env; gitignored
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",        # silently ignore unknown env vars (e.g. PATH)
        # Pydantic v2: validate on init; immutable afterwards
        frozen=True,
    )

    app: AppSettings = Field(default_factory=AppSettings)
    mongo: MongoSettings = Field(default_factory=MongoSettings)
    kafka: KafkaSettings = Field(default_factory=KafkaSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    vector: VectorSettings = Field(default_factory=VectorSettings)
    blob: BlobSettings = Field(default_factory=BlobSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    gemini: GeminiSettings = Field(default_factory=GeminiSettings)
    otel: OTelSettings = Field(default_factory=OTelSettings)
    langsmith: LangSmithSettings = Field(default_factory=LangSmithSettings)
    eval: EvalSettings = Field(default_factory=EvalSettings)
    api: APISettings = Field(default_factory=APISettings)
    ingest: IngestSettings = Field(default_factory=IngestSettings)
    analysis: AnalysisSettings = Field(default_factory=AnalysisSettings)
    sse: SSESettings = Field(default_factory=SSESettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            _CsvFriendlyEnvSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
        )


# ---------------------------------------------------------------------------
# Public accessor — lru_cache makes this a process-wide singleton
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton Settings instance.

    Cached so all callers see the same object. Tests can clear the cache via
    `get_settings.cache_clear()` and inject their own Settings.
    """
    return Settings()
