# Phase 14 — Python SDK

## What was built

A production-quality Python SDK for the TrendStorm AI REST API, alongside a shared
models package that captures the wire format as a dependency both the server and SDK
can import. The SDK ships with full async and sync surfaces, typed SSE streaming,
automatic retry with backoff, OAuth 2.0 support with auto-refresh, HITL review
operations, and a MkDocs-material documentation site.

## New packages

### `packages/trendstorm-shared` (PyPI: `trendstorm-shared`)

Extracts API contract types from the server domain into a versioned package:
- `trendstorm_shared.types` — all enums (`JobStatus`, `SourceType`, `ReportFormat`,
  `ReviewStatus`, `ReviewDecision`, `StreamEventType`) with their `is_terminal` /
  `is_resolved` properties matching the server's StrEnum definitions.
- `trendstorm_shared.models` — Pydantic v2 models for every request/response
  in the API surface. `extra="ignore"` is used (not `"forbid"`) so the SDK is
  forward-compatible with new optional server fields without requiring a patch release.

Server and SDK both depend on `trendstorm-shared`. The shared package is versioned
independently; a semver breaking change in the shared models requires bumping both
the server's and SDK's declared dependency range.

### `sdk/python` (PyPI: `trendstorm`)

Import as `trendstorm_sdk`. Key modules:

- `_client.py` — `TrendStormClient` async context manager. Owns the httpx session
  lifecycle, delegates per-resource calls, and handles response parsing + error mapping.
- `_auth.py` — `ApiKeyAuth` (static Bearer key) and `OAuthAuth` (Bearer token with
  asyncio-safe auto-refresh behind a lock). Auth strategy is swappable.
- `_errors.py` — Full error hierarchy. `APIError.from_response()` dispatches to the
  right subclass by status code. Every error carries `status_code`, `error_code`,
  `message`, `request_id`, `correlation_id`, and `raw` body dict.
- `_retry.py` — `retry_request()` function (not a transport subclass) wraps any
  `httpx.AsyncClient.request` coroutine. Retries 429/5xx with exponential backoff
  (1s, 2s, 4s … cap 60s); parses integer-seconds or HTTP-date `Retry-After` headers.
  Network errors (`ConnectError`, `ReadTimeout`, `RemoteProtocolError`) also retry.
  4xx (except 429) raise immediately.
- `_sse.py` — `SSEStream` async iterator. Implements SSE parsing (comment suppression,
  multi-line data joining, frame dispatch on empty line) over httpx streaming without
  any SSE library dependency. Reconnects on `httpx.StreamError` / `ReadTimeout` up to
  `max_reconnects` (default 3), forwarding `Last-Event-ID` (the seq integer) on each
  reconnect. Heartbeat timeout enforced via `asyncio.wait_for` per line.
- `_sync.py` — `SyncTrendStormClient` wraps the async client. Uses `asyncio.run()`
  per method call — correct for scripts, CLI tools, Celery tasks. Not for high-
  throughput code: each call creates a new event loop. SSE streaming is deliberately
  absent from the sync client (blocking generators over async iterators are unsound).
- `resources/` — one file per API surface (`categories.py`, `sources.py`, `jobs.py`,
  `reviews.py`, `quota.py`, `api_keys.py`). Each subclasses `AsyncAPIResource` and
  calls the typed `_get/_post/_patch/_delete` helpers. All methods return typed shared
  models, never raw dicts.

## Non-obvious decisions

**`extra="ignore"` on shared models (not `"forbid"`).** The server domain models use
`"forbid"` for strict internal validation. The SDK models flip this to `"ignore"` so
that a server-side API extension (new optional field in a response) doesn't break an
older SDK version. The SDK is the consumer; it should degrade gracefully. This is the
opposite of the server's convention and is intentional.

**Retry is a function, not a transport subclass.** The `retry_request()` helper wraps
a zero-argument coroutine rather than subclassing `httpx.AsyncBaseTransport`. This keeps
SSE streaming fully decoupled: the `SSEStream` class calls `client.stream()` directly,
without the retry logic firing on streaming responses (where retry is meaningless and
would eat events). A transport-level retry would intercept streaming responses too.

**`asyncio.run()` per method in `SyncTrendStormClient`, not one persistent loop.**
Using a persistent loop (e.g. `loop.run_until_complete`) requires the caller to manage
loop lifecycle, which clashes with frameworks (FastAPI, Celery, Jupyter). `asyncio.run()`
is self-contained and always safe. The cost — one event-loop creation per call — is
acceptable for the sync client's intended use cases (CLI scripts, one-off queries).

**SSE parser is custom, not a library.** `aiohttp-sse-client`, `sseclient-py`, and
`httpx-sse` all handle the common case but fail on at least one of: `Last-Event-ID`
forwarding on reconnect, typed payload model validation, heartbeat-per-line timeout, or
`httpx` streaming integration. Writing ~120 lines of parser code is cheaper than
maintaining compatibility shims for a library that's not a perfect fit.

**`max_reconnects=3` by default.** Three reconnects covers transient flaps (network
hiccup, load balancer rotation) without masking systematic failures (server down, auth
revoked). After three failures the `StreamError` surfaces so the caller can decide.

**Shared models are `extra="ignore"` but the server routers' schemas remain `extra="forbid"`.** The
server never imports `trendstorm_shared` for its own request schemas — it defines them
locally in each router module. The shared package is consumed by the SDK and by any
test that validates the wire format. This avoids adding a runtime dependency cycle.

## Directory layout added

```
packages/
  trendstorm-shared/
    pyproject.toml
    src/trendstorm_shared/__init__.py
    src/trendstorm_shared/types.py
    src/trendstorm_shared/models.py

sdk/python/
  pyproject.toml
  README.md
  CHANGELOG.md
  src/trendstorm_sdk/
    __init__.py
    _client.py
    _auth.py
    _errors.py
    _retry.py
    _sse.py
    _sync.py
    resources/
      __init__.py
      _base.py
      categories.py
      sources.py
      jobs.py
      reviews.py
      quota.py
      api_keys.py
  examples/
    quickstart.py
    hitl_reviewer.py
    cost_dashboard.py
  tests/
    unit/
      conftest.py
      test_auth.py
      test_errors.py
      test_retry.py
      test_sse.py
      test_client.py
    integration/
      conftest.py
      test_categories.py
      test_jobs.py
  docs/
    mkdocs.yml
    docs/{index,quickstart,authentication,streaming,hitl,error-handling,api-reference}.md

.github/workflows/sdk-release.yml
```

## Publishing

Release workflow at `.github/workflows/sdk-release.yml` triggers on `sdk-v*` tags.
Uses PyPI trusted publishing (no token in secrets). Builds `trendstorm-shared` first,
then `trendstorm` (SDK), publishing both. Docs are built and uploaded as an artifact;
deploy to `docs.trendstorm.io/sdk/python` is a TODO (target depends on hosting choice).

Tag convention: `sdk-v0.1.0` → publishes both packages at `0.1.0`. If only the shared
package changes, tag `shared-v0.1.1` and publish it independently (workflow can be
split if needed).
