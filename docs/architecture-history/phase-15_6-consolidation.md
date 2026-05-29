# Phase 15.6 — Consolidation: Schema Unification, Lint/Type Clean, Dashboard Semantic Fixes

## Goals

Phase 15.6 was a consolidation sprint with no new features. Three objectives:
1. Unify the API wire format so server, SDK, and dashboard share a single source of truth (`trendstorm-shared`).
2. Achieve a clean static analysis gate: ruff + mypy strict across all Python source trees, TypeScript strict across the dashboard.
3. Fix all phantom/incorrect field references in dashboard pages so the UI renders real data.

---

## A. Schema Unification

### Problem
Three codebases had independently evolved their type definitions for the same API contract:
- Server routers defined inline Pydantic response models that duplicated `trendstorm-shared` types.
- The SDK imported `trendstorm-shared` but the server didn't, so field renames in `trendstorm-shared` weren't caught at server build time.
- The dashboard `types.generated.ts` was missing several types (`Page<T>`, `QuotaUsage`, `Citation`) and had a phantom `QuotaStatus` interface with wrong field names.

### Solution
All server routers (`jobs`, `sources`, `categories`, `reviews`, `memories`, `audit`) now import enums and request/response types from `trendstorm-shared`. The server does NOT import `trendstorm-shared` at the package level — it uses it only at the router boundary to parse request bodies and construct response dicts, keeping the hexagonal boundary intact (domain models stay in `src/trendstorm/domain/`).

`web/dashboard/src/api/types.generated.ts` was updated to add missing types and remove the phantom `QuotaStatus`. The committed baseline is the canonical schema snapshot; `npm run codegen` regenerates it from `/v1/openapi.json`.

`GET /v1/audit` was added — previously the `audit_log` collection was write-only from the system's perspective; now operators can page through events in the dashboard.

### SDK dependency resolution
The SDK (`sdk/python/`) depends on `trendstorm-shared` but the main project also uses the package name `trendstorm`. Joining the uv workspace was blocked by the name conflict. Solution: add `[tool.uv.sources]` to `sdk/python/pyproject.toml` pointing directly to `../../packages/trendstorm-shared`. This resolves the local dep without workspace membership and without publishing to PyPI.

---

## B. Lint and Type Clean

### Python (ruff + mypy strict)

Ruff additions to `pyproject.toml` global ignores:
- `B008`: FastAPI `Query()` / `Depends()` in default args is idiomatic — not a bug.
- `S106`: False-positive on `redact_token="[REDACTED:X]"` strings (ruff thinks these are hardcoded passwords).
- `S110` / `SIM105`: `try/except Exception: pass` blocks are intentional for fire-and-forget metric recording per Rule 53. Using `contextlib.suppress` would add noise without benefit.

mypy fixes across all source dirs:
- `MemoryRepository` Protocol gained `exists_for_job` (was on the concrete implementation only).
- `EmbeddingBatchResult` is not subscriptable directly — all memory services corrected to `.vectors[0]`.
- `require_role()` return type changed to `Any` (FastAPI `Depends` is not a valid mypy type annotation).
- Dual `ReviewDecision` enums (`trendstorm_shared.types` vs `trendstorm.domain.reviews.models`) required explicit conversion at the router boundary: `DomainReviewDecision(body.decision.value)`.
- `dict` → `dict[str, object]` on several `update`/`query` local variables in repositories.
- `memory_consolidation_worker.py`: corrected `build_chat_provider`/`build_embedding_provider` (not `get_*`), `producer.producer.send_and_wait` (not `producer.send_and_wait`), `MetricsServer.start()`/`.stop()` (not `.run()`).
- `auth/service.py`: roles extraction now handles `str | list[str] | None` from JWT claims.

### TypeScript (tsc strict)

Dashboard pages had drifted from the actual API contract — many fields were phantom (never existed on the server):

| Page | Phantom fields removed | Correct fields used |
|---|---|---|
| `Jobs.tsx` | `job.job_id`, `refinement_loops_used`, `cost_usd` | `job.id`, `job.metrics.*` |
| `JobDetail.tsx` | `job.job_id`, `job.error_message`, phantom PDF/JSON URL fields | `job.id`, `job.failure_message`, `job.report_id` |
| `JobReport.tsx` | `reportContentOptions` (no URL field exists) | Analysis summary from `/v1/jobs/{id}/analysis` |
| `CategoryDetail.tsx` | `job.job_id`, `job.cost_usd` | `job.id`, `job.metrics.documents_ingested` |
| `Usage.tsx` | `current_usd`, `hard_cap_usd`, `daily_breakdown`, `by_stage`, `by_provider`, `period_start`, `period_end` (ALL phantom) | `QuotaUsage.monthly_spend_usd`, `.monthly_limit_usd`, `.jobs_this_month`, `.jobs_limit`, `.allowed`, `.reason` |

`StatusBadge.tsx` and `PipelineProgress.tsx` used `ingested` and `embedded` (not in `JobStatus` enum). Corrected to actual values; `memory_consolidation` added.

`Page<T>` adapter normalization: server returns `{ jobs: [...] }` / `{ sources: [...] }` / `{ categories: [...] }` but TanStack Query factories expected `{ items: T[], next_cursor }`. Adapter transforms added in each `queryFn`.

Quota query: endpoint corrected from `/v1/billing/quota` → `/v1/quota`.

---

## C. New Endpoint: GET /v1/audit

`src/trendstorm/api/routers/audit.py` — cursor-paginated audit log endpoint. Parameters: `before_id` (ULID cursor), `event_type` (optional filter), `limit` (max 100, default 25). Response: `AuditLogList` with `items` and `next_cursor`. Requires `tenant_admin` role. Router registered in `api/main.py:create_app`.

`web/dashboard/src/pages/AuditLog.tsx` — infinite-scroll table showing `event_type`, `actor`, `resource_type`, `action`, `outcome`, `created_at`. Linked from sidebar. Query factory at `src/api/queries/audit.ts`.

---

## Test counts after Phase 15.6

| Suite | Count | Command |
|---|---|---|
| Python unit | 1116 | `pytest -m unit` |
| SDK unit | 51 | `cd sdk/python && pytest -m unit` |
| Dashboard unit | 48 | `cd web/dashboard && npm test` |
| TypeScript typecheck | clean | `cd web/dashboard && npm run typecheck` |

Integration tests (`-m integration`) require `make up` and are unaffected by this phase.
