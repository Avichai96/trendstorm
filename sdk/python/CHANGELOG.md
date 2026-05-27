# Changelog

All notable changes to the TrendStorm Python SDK are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] — 2026-05-27

Initial release, shipping alongside TrendStorm AI v1.0 (Phases 1–14).

### Added
- `TrendStormClient` — async context manager wrapping the TrendStorm REST API
- `SyncTrendStormClient` — synchronous facade for scripts and CLI tools
- Resources: `categories`, `sources`, `jobs`, `reviews`, `quota`, `api_keys`
- SSE streaming via `ts.jobs.stream()` — typed `StreamEvent` objects,
  `Last-Event-ID` resumption, automatic reconnect (≤3 attempts), heartbeat timeout
- Auth: API key (`Authorization: Bearer ts_live_*`) and OAuth 2.0 with auto-refresh
- Retry: exponential backoff for 429/5xx; `Retry-After` header respected;
  network errors retried up to `max_retries` (default 5)
- Error hierarchy: `TrendStormError → APIError → (RateLimited, NotFound,
  Unauthorized, ValidationError, ServerError)` + `ConfigurationError`,
  `StreamError`, `HeartbeatTimeout`
- HITL review methods: `approve`, `reject`, `request_refinement`
- `trendstorm-shared` package: shared Pydantic models and enums for server + SDK
- Examples: `quickstart.py`, `hitl_reviewer.py`, `cost_dashboard.py`
- MkDocs-material documentation site at `sdk/python/docs/`
- GitHub Actions trusted publishing workflow (`sdk-v*` tag trigger)
