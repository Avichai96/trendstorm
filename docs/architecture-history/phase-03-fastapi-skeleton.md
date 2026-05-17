# Phase 3 — FastAPI Production Skeleton

**Status**: ✅ Complete

## Summary

`shared/config` — nested Pydantic Settings with `__`-delimiter env mapping, `SecretStr` for secrets, `frozen=True`, `lru_cache`'d `get_settings()`. Custom `_CsvFriendlyEnvSource` allows CSV fallback for list fields. Loads `.env` THEN `.env.local`.
`shared/logging` — structlog + stdlib bridge, contextvars for correlation_id/tenant_id, OTel trace_id/span_id auto-injection, sensitive key redaction.
`shared/tracing` — OTel auto-instrumentation (FastAPI/pymongo/redis/httpx/logging), `ParentBased + TraceIdRatio` sampler, OTLP gRPC.
`shared/errors` — hierarchy `TrendStormError → {Config, Validation, NotFound, Conflict, BusinessRule, ExternalService → {Database, Broker, LLM → {LLMRateLimit, LLMTimeout}}}`.
`infrastructure/{mongo,redis,kafka}` — lifecycle-managed client wrappers (connect/close, idempotent, health_check).
`api/main.py` — app factory + lifespan that connects all clients in parallel, attaches to `app.state`. Middleware order (outer→inner): CorrelationId → RequestLogging → Tenant → CORS. Lifespan guarantees full cleanup on partial startup failure.
`api/middleware/{correlation,tenant,request_logging}` — correlation generates/echoes ULID; tenant requires `X-Tenant-ID` (except public paths); request_logging emits one structured line per request.
`api/routers/health` — separate `/health/live` (trivial) and `/health/ready` (parallel dep checks, 503 on failure).
`api/error_handlers` — maps domain exceptions to HTTP statuses with `{error: {code, message, context}, correlation_id}` envelope. Uses `HTTP_422_UNPROCESSABLE_CONTENT` (renamed in current Starlette).
