# TrendStorm AI — Engineering Context (CLAUDE.md)

> Context handoff document. Read fully before generating code.
> Project root: `/Users/avichaicohen/Projects/trendstorm/`

---

## 1. Project Overview & Architecture

### What this is
**TrendStorm AI** — an autonomous multi-agent trend intelligence platform. A tenant defines a *category* (e.g. "AI safety") with registered *sources* (URLs, RSS, APIs). A *job* fans those sources out through a pipeline of agents (Scout → Knowledge → Analyst → Publisher), producing a cited, structured trend report streamed to the user in near-real-time. Built for production scale, not demo scale.

### High-level architecture
- **Event-driven** via Kafka — decoupled stages, replay, isolated failure domains.
- **Hexagonal architecture** — `domain/` defines Protocols; `infrastructure/` implements them; `services/` composes use cases; `api/` is the FastAPI HTTP layer; `agents/` and `orchestration/` hold the LangGraph pipeline.
- **Thin state, fat store** — LangGraph `JobState` carries references (IDs, blob URIs), never bulk payloads. Real content lives in Mongo / MinIO / ChromaDB.
- **At-least-once Kafka + idempotency keys + LangGraph checkpoints → effective exactly-once user view.**
- **Multi-tenant from day one** — every collection (except `idempotency`) has `tenant_id`; every index starts with `tenant_id`; every repository query funnels through `_tenant_query()`.

### Tech stack (locked in)
- **Python 3.12+**, `uv` for deps & venv, `pyproject.toml` (PEP 621).
- **FastAPI** + Uvicorn, **Pydantic v2** + pydantic-settings, **sse-starlette**.
- **MongoDB** (single-node replica set in dev; replica set is required for LangGraph transactions). **Motor** for async; **pymongo sync** for the LangGraph `MongoDBSaver` only.
- **Kafka KRaft** mode (no Zookeeper), **aiokafka**, `lz4`+`cramjam` compression.
- **Redis** (semantic cache, rate limiting, idempotency staging).
- **ChromaDB** (pluggable for Pinecone) — vectors only; chunk text stays in Mongo.
- **MinIO** (S3-compatible) — raw HTML, parsed text, rendered reports.
- **Ollama** local LLMs (`llama3.2:3b`, `nomic-embed-text:latest`); Anthropic/OpenAI/Cohere for prod paths.
- **LangGraph 0.2.x** with MongoDB checkpointer.
- **OpenTelemetry** auto-instrumentation → OTLP gRPC → Collector → Jaeger/Loki/Prometheus → Grafana. **structlog** with contextvars for correlation_id propagation.
- **ULID** for all IDs (sortable, distributed-safe, 26 chars).

### Deployable units
- `trendstorm-api` (FastAPI, port **8080**).
- `trendstorm-orchestrator` (Kafka consumer driving LangGraph).
- Phase 6+: `trendstorm-scout`, `trendstorm-knowledge`, `trendstorm-analyst`, `trendstorm-publisher`, `trendstorm-sse-coordinator` (each is its own Kafka worker).
- Phase 11+: `trendstorm-production-eval` (1% sample eval worker).
- Phase 13.5+: `trendstorm-review-timeout` (polling sweeper, single replica Recreate).
- Phase 14+: **Python SDK** at `sdk/python/` (PyPI: `trendstorm`); shared models at `packages/trendstorm-shared/` (PyPI: `trendstorm-shared`).
- Phase 15a+: **Dashboard SPA** at `web/dashboard/` (Vite + React 18 + TypeScript); served static via nginx / S3+CloudFront. Separate Helm chart at `helm/dashboard/`. Not in the 10-service count — it is a CDN-served static artifact, not a running server process.

### Frontend tech stack (Phase 15a+)
- **Vite 5** bundler, **React 18** + **TypeScript strict**, **React Router 6**, **TanStack Query v5**.
- **Auth0 React SDK** — OAuth2 universal login; custom JWT claims for roles and tenants.
- **Tailwind v3** + **shadcn/ui** primitives (Radix-UI backed, full component ownership).
- **Recharts** for cost charts; **react-markdown** + remark-gfm for report rendering.
- **Custom SSE client** (`src/lib/sse.ts`) — fetch-based, supports `Authorization:` headers (native `EventSource` does not).
- **Runtime config** — `config.json` served from CDN/ConfigMap (not build-time env vars) so the same image deploys to staging and production with different Auth0 clients.

### Directory layout
```
src/trendstorm/
  shared/          config, logging, tracing, errors, ids, types       (Phase 3)
  api/             main, deps, middleware, routers, error_handlers   (Phase 3+)
  infrastructure/
    mongo/         client, schema, indexes, repositories/             (Phase 3-5)
    kafka/         producer, consumer (BaseConsumer)                  (Phase 3-4)
    redis/         client                                             (Phase 3)
    llm/           gemini, openai, ollama, retry, registry, anthropic (Phase 7)
    vectors/       chroma_store                                       (Phase 7)
    blob/          minio_client (+download), uri                      (Phase 6-7)
    security/      ssrf.py, sanitize.py, pii.py, blocklist.py        (Phase 13)
  domain/          jobs, categories, sources, documents, chunks,
                   analyses, reports (each: models.py, repository.py) (Phase 4-5)
                   llm/{providers,models}, vectors/{store,models}     (Phase 7)
                   evaluation/{models,evaluator,judge}                (Phase 11)
                   audit_log/{models,repository}, url_blocklists/{models,repository} (Phase 13)
                   reviews/{models,repository}, tenant_settings/{models,repository} (Phase 13.5)
  agents/          stages, state, orchestrator/{nodes,edges,graph,checkpointer}  (Phase 4)
                   knowledge/{tokenizer,chunker,pipeline}             (Phase 7)
                   production_eval/pipeline                           (Phase 11)
  orchestration/   topics, events, workers/orchestrator_worker        (Phase 4)
                   workers/review_timeout_worker                      (Phase 13.5)
  services/        job_service, category_service, source_service      (Phase 4-5)
  utils/           headers_docs.py (OpenAPI documentation helpers)
tests/
  unit/            pure functions, no I/O                              (-m unit)
  integration/     full stack via `make up`                            (-m integration)
docker/            docker-compose.{yml,obs,dev,app}.yml + Dockerfiles
scripts/           healthcheck.py, smoke_test.py, seed_mongo_indexes.py
packages/
  trendstorm-shared/  shared API contract types (enums + models)      (Phase 14)
sdk/
  python/
    src/trendstorm_sdk/  async + sync client, SSE, retry, auth, errors (Phase 14)
    examples/            quickstart.py, hitl_reviewer.py, cost_dashboard.py
web/
  dashboard/             Vite SPA (Phase 15a)
    src/api/             fetch client, TanStack Query options, generated types
    src/auth/            AuthGuard, RoleGuard
    src/components/ui/   shadcn/ui primitives
    src/components/layout/  AppShell, Sidebar, TenantSelector
    src/components/jobs/ PipelineProgress
    src/components/reviews/ SlaCountdown, DecisionForm
    src/components/reports/ MarkdownViewer, CitationPanel
    src/hooks/           useSSE, useTenant
    src/lib/             utils, sse (fetch-based SSE client)
    src/pages/           Categories, CategoryDetail, Jobs, JobDetail, JobReport
                         Reviews, ReviewDetail, Usage, AuditLog
    tests/unit/          Vitest + RTL component tests + axe a11y
    tests/e2e/           Playwright (login, jobs, reviews)
helm/
  dashboard/             Helm chart: nginx Deployment + ConfigMap + Ingress (Phase 15a)
    tests/unit/          SDK unit tests (respx mocks, no network)
    tests/integration/   SDK integration tests (live API required)
    docs/                MkDocs-material documentation site
```

---

## 2. Current State of the System

TrendStorm is production-ready through Phase 15a. The full pipeline runs end-to-end with security hardening and human review gating, a versioned Python SDK, and a live operator dashboard:
POST /v1/jobs → orchestrator → scout (SSRF-validated) → knowledge (PII-redacted) → analyst (injection-contained) → [review gate: approve/reject/refine] → publisher (sanitized) → SSE delivery.

### Deployable services (10)

| Service | Purpose | Scaling signal |
|---|---|---|
| api | HTTP + SSE (port 8080) | CPU + connections |
| orchestrator-worker | LangGraph state machine | Kafka lag |
| scout-worker | Source fetching + parsing | Kafka lag |
| knowledge-worker | Chunking + embedding | Kafka lag |
| analyst-worker | Hybrid RAG + LLM analysis | Kafka lag |
| publisher-worker | Markdown/PDF/JSON rendering | Kafka lag |
| sse-coordinator-worker | Kafka → Redis Streams fanout | Kafka lag |
| production-eval-worker | 1% production sample eval | Kafka lag |
| outbox-relay-worker | Mongo outbox → Kafka | 1-2 replicas (Recreate) |
| review-timeout-worker | Auto-expire pending reviews past SLA | N/A (single replica Recreate) |

### Phase completion history

All phases through 15a are complete. For detailed per-phase architecture decisions, see [docs/architecture-history/](docs/architecture-history/).

| Phase | Title | Doc |
|---|---|---|
| 1 | Architecture design | [phase-01](docs/architecture-history/phase-01-architecture.md) |
| 2 | Local infrastructure | [phase-02](docs/architecture-history/phase-02-local-infra.md) |
| 3 | FastAPI production skeleton | [phase-03](docs/architecture-history/phase-03-fastapi-skeleton.md) |
| 4 | LangGraph orchestration backbone | [phase-04](docs/architecture-history/phase-04-langgraph-orchestration.md) |
| 5 | MongoDB schema deep dive | [phase-05](docs/architecture-history/phase-05-mongodb-schema.md) |
| 6 | Scout Agent (ingestion pipeline) | [phase-06](docs/architecture-history/phase-06-scout-agent.md) |
| 7 | Knowledge Agent (chunking + embedding) | [phase-07](docs/architecture-history/phase-07-knowledge-agent.md) |
| 8 | Hybrid retrieval + Analyst agent | [phase-08](docs/architecture-history/phase-08-hybrid-retrieval-analyst.md) |
| 9 | Streaming + Publisher Agent | [phase-09](docs/architecture-history/phase-09-streaming-publisher.md) |
| 10 | Observability deep dive | [phase-10](docs/architecture-history/phase-10-observability.md) |
| 11 | Evaluation pipeline | [phase-11](docs/architecture-history/phase-11-evaluation.md) |
| 12 | Production readiness | [phase-12](docs/architecture-history/phase-12-production-readiness.md) |
| 13 | Prompt injection defense & SSRF hardening | [phase-13](docs/architecture-history/phase-13-security-hardening.md) |
| 13.5 | Human-in-the-loop review queue | [phase-13.5](docs/architecture-history/phase-13_5-hitl.md) |
| 14 | Python SDK + shared models package | [phase-14](docs/architecture-history/phase-14-sdk.md) |
| 15a | Dashboard SPA + HITL review UI | [phase-15a](docs/architecture-history/phase-15a-dashboard.md) |

### What works end-to-end

- `make up` → infra (Mongo replica set, Kafka KRaft, Redis, ChromaDB, MinIO, Ollama)
- `make seed-indexes` → all Mongo indexes (idempotent, includes `audit_log` and `url_blocklists`)
- `make up-obs` → OTel Collector, Jaeger, Prometheus, Loki, Grafana (trendstorm-overview dashboard)
- `make up-app` → api + all 10 workers (each exposes `/metrics` on port 9090)
- Full distributed trace via OTel, Prometheus metrics per stage, structured logs via Loki
- Test suites: `tests/unit/` (1046 tests, no Docker); `tests/integration/` exercises real infra; `sdk/python/tests/unit/` SDK unit tests (respx mocks)
- SSRF validation on every Scout URL (initial + each redirect hop, max 3 hops)
- Global blocklist loaded from `ops/security/global-blocklist.txt`; per-tenant blocklist in `url_blocklists` collection
- Chunk content wrapped in `<chunk>` tags with explicit data/instruction boundary rules in analyst prompt
- Validator `injection_resistance` dimension (weight 0.10) detects and penalizes hijacked analyses
- PII detection via `DefaultPIIDetector` (SSN, CC, email, phone, IBAN) before LLM submission
- Output sanitization strips XSS vectors from analysis text before persistence
- `audit_log` collection records all SSRF blocks, PII detections, and blocklist hits (365-day TTL)
- `trendstorm_security_block_total{reason, tenant_id_hash}` Prometheus counter for all security events
- 5 adversarial golden examples in `eval/golden/adversarial/` covering injection, role-reassignment, exfiltration, SSRF-in-HTML
- HITL review queue: per-tenant modes (off/always/flagged_only), 48h SLA with auto-reject sweeper, reviewer role on API keys, atomic Mongo transaction for resolve via outbox pattern
- `Stage.AWAITING_REVIEW` and `Stage.REJECTED` (terminal, distinct from FAILED); `skip_hitl_gate` guard on JobState prevents re-gating after review resolution
- `/v1/reviews` API (list, get, resolve) gated by `require_role("reviewer")`; `PendingReviewsAgingHigh` alert fires at 80% of SLA

### Architecture Decision Records

ADRs are in [docs/adr/](docs/adr/). See `docs/adr/001-single-region-deployment.md` for the first record.

### Next steps

See [Section 3](#3-next-steps--pending-tasks) below.

---

## 3. Next Steps & Pending Tasks

### Phase 15a (complete)
Dashboard SPA at `web/dashboard/` — Vite + React 18 + TypeScript strict, Auth0, TanStack Query v5, shadcn/ui, Recharts. Read-only views for categories, sources, jobs, reports, usage, audit log. HITL review queue with full decision form (approve / reject / request_refinement). Live SSE job progress via custom fetch-based client. Helm chart at `helm/dashboard/`.

### Phase 15b (next — not started)
Dashboard write paths: category/source creation, API key management UI. Auth0 Action setup wizard.

### Phase 16 (not started)
Multi-region deployment + data residency (ADR 001 trigger conditions), infrastructure-as-code (**Terraform**), Velero backups.

### Pending polish items
- **RetryingChatProvider**: `RetryingEmbeddingProvider` covers embeddings; consider an equivalent wrapper for transient errors on the Analyst hot path (separate from Kafka-level retry which is too coarse for multi-step LLM flows). Runbook [llm-rate-limit.md](ops/runbooks/llm-rate-limit.md) calls this out as prevention.
- **Kafka consumer lag metric**: `BaseConsumer` does not yet update `METRICS.kafka_consumer_lag` gauge — each worker needs to call it on every poll cycle with its current lag.
- **ChromaDB health gauge**: `trendstorm_vector_store_health` is only set at startup. Workers that use ChromaDB should refresh it on a 30s interval via a background task.
- **`publish_node` stage transition**: bypasses `_record_transition` for `PUBLISHING → COMPLETED` — route through the helper for consistency.
- **Apply `require_tenant` to jobs + sources routers**: Phase 5 only applied the OpenAPI documentation dependency to categories.
- **`frozen=True`** on remaining nested Settings models for consistency.
- **`test_create_job_drives_to_completed`** remains skipped — orchestrator-only fixture can't drive to COMPLETED without all workers; `test_full_pipeline_with_sse.py` is now the canonical full-stack regression.
- **`scripts/smoke_test.py`** Mongo session test has Motor API drift (`TypeError: object AsyncClientSession can't be used in 'await' expression`). Fix when next touching the smoke script.
- **Token-by-token streaming from the Analyst**: the Analyst uses `complete_with_tools` (atomic). Future: switch to `chat.stream()` and forward deltas as `CHUNK_DELTA` stream events during generation, not just after.
- **`$merge` rollups**: `jobs_daily_stats` Mongo aggregation — pre-aggregation for heavy dashboard queries.

---

## 4. Specific Code Patterns & Rules

### Hard rules (do not deviate without explicit discussion)

1. **Hexagonal layering — dependencies point inward only.**
   `domain/` defines Protocols, knows nothing about Mongo/Kafka/HTTP.
   `infrastructure/` implements Protocols, depends on domain.
   `services/` composes domain Protocols into use cases.
   `api/` and `orchestration/workers/` are the outermost layer.
   **Never** import from `infrastructure/` inside `domain/`.

2. **Repositories are NOT generic CRUD wrappers.** Subclass `TenantScopedRepository[T]` and use its primitives (`_tenant_query`, `_insert`, `_find_one`, `_find_many`, `_encode`, `_decode`), but the public method names must read like business intent (`list_pending_for_tenant`), not raw queries (`find`). No `find/update/delete` generic methods.

3. **`_tenant_query()` is the ONLY function that constructs Mongo filters for tenant-scoped collections.** Every query — including by `_id` — funnels through it. This makes tenant-bypass bugs impossible by construction. The only exempt collection is `idempotency`.

4. **Collection names ONLY via `Collection` enum** in `infrastructure/mongo/schema.py`. Never string literals at call sites.

5. **Index definitions ONLY in `infrastructure/mongo/indexes.INDEXES`.** The seeder reads from there. Adding an index = appending an `IndexSpec`. Tests assert structural invariants (no dup names, TTL is single-field, unique indexes are tenant-prefixed).

6. **All Pydantic models have `model_config = ConfigDict(extra="forbid")`** unless deliberately accepting external data (then `extra="ignore"` with a comment). Closed schemas catch typos at validation.

7. **Identity is ULID, 26 chars.** `new_id()` from `shared.ids`. Mongo `_id` is the same string as the model's `id` field — translated by `to_mongo_doc`/`from_mongo_doc` in `_base.py`. No ObjectIds, no auto-increment, no UUIDs.

8. **Timestamps are timezone-aware UTC.** `now_utc()` from `infrastructure.mongo.repositories._base`. Never `datetime.utcnow()` (returns naive — silently breaks comparisons).

9. **Settings are immutable.** Root `Settings` has `frozen=True`. To change config: restart process (twelve-factor). Tests clear `get_settings.cache_clear()` and instantiate fresh.

10. **Secrets are `SecretStr`.** Unwrap with `.get_secret_value()` only where actually needed. The logging redaction processor in `shared/logging` is the second layer.

11. **Logging is structlog + stdlib bridge.** `from trendstorm.shared.logging import get_logger`. Module-level: `logger = get_logger(__name__)`. Bind context via `bind_context(correlation_id=..., tenant_id=...)` in middleware/worker entry points. Never `print()`. Never plain `logging.getLogger()` without going through our config.

12. **Tracing is OTel auto-instrumentation + targeted manual spans.** `tracer = trace.get_tracer(__name__)` at module level. Wrap business-meaningful units (`with tracer.start_as_current_span("category.create")`); don't wrap every method.

13. **Kafka producer is idempotent**: `acks="all"`, `enable_idempotence=True`, `compression_type="lz4"`. **Consumer is manual-commit**: `enable_auto_commit=False`, commit after successful handler. **Idempotency keys** use `{job_id}:{stage}:{attempt}`. The orchestrator worker is the only worker that opts out (returns `None` from `_idempotency_key` because LangGraph checkpoints serve that role).

14. **LangGraph state is thin.** References (IDs, blob URIs, hashes), never bulk content. Bulk content lives in Mongo (chunks/analyses), MinIO (raw HTML, reports), ChromaDB (vectors).

15. **LangGraph checkpointer uses sync `MongoDBSaver`** with its own pymongo connection (NOT motor, NOT `AsyncMongoDBSaver`). Constructor takes `MongoSettings`, not the shared `MongoClient`. This is operationally fine — two pools per worker process. Documented in `agents/orchestrator/checkpointer.py`.

16. **Stage transitions go through `is_valid_transition()`.** Defined explicitly in `agents/stages.py`. Self-loops are allowed (retry). `ANALYZING → RETRIEVING` is the only backward edge (refinement). Never bypass the check unless the call site documents why (`publish_node` does for the entry-path / exit-path split).

17. **Routers use `dependencies=[Depends(require_tenant)]`** from `utils/headers_docs.py` so the `X-Tenant-ID` header appears in OpenAPI/Swagger. The `TenantMiddleware` still does the real enforcement; `require_tenant` is documentation only.

18. **Error envelope is sacred**: `{"error": {"code": "...", "message": "...", "context": {...}}, "correlation_id": "..."}`. SDKs depend on this shape. Add to it; never break it.

19. **Two cleanups for any compose change**: `docker-compose.yml` (infra) is what `make up` brings up; `docker-compose.app.yml` (api + workers) overlays on top and joins the external network `trendstorm_trendstorm`. Don't put workers in the base file.

20. **API port is 8080** everywhere (Makefile, Dockerfiles, healthcheck). Not 8000.

21. **Async-resume via Kafka (the handoff pattern).** When a LangGraph node needs to delegate work to an external worker (scout, knowledge, etc.): (a) the node publishes an event and returns partial state WITHOUT advancing the stage — the graph pauses via `interrupt_after=[NODE_NAME]` passed at `astream()` call time; (b) the downstream worker processes the work and publishes a completion event; (c) `OrchestratorWorker._handle_*_completed` calls `aupdate_state(config, update, as_node=NODE_NAME)` then `astream(None, config)` to resume. This is the canonical pattern for all agent handoffs. `interrupt_after` is passed at call time, not compile time — this preserves unit-test compatibility (stub path runs without LangGraph interrupts).

22. **`ingest_node` (and future handoff nodes) are dual-mode.** Check `config.get("configurable", {}).get("kafka_producer")` — if `None`, execute the stub path so unit tests pass without infrastructure; if present, execute the production Kafka-publish path. Never remove the stub path; it is load-bearing for the unit test suite.

23. **MinIO uploads use `aioboto3`, not `minio-py`.** `MinioClient` (`infrastructure/blob/minio_client.py`) enters an `aioboto3.Session().resource("s3")` context manager in `connect()`. Blob keys follow `{tenant_id}/{job_id}/{doc_id}/{raw.html|text.txt|report.md}` — pure-function builders in `infrastructure/blob/uri.py`. Always use `to_s3_uri` / `parse_s3_uri` for `s3://` URI construction; never hand-format.

24. **Scout concurrency uses `asyncio.Queue` producer-consumer, not `asyncio.gather`.** The queue pattern bounds concurrency, handles back-pressure, and allows sitemaps discovered mid-run to push new tasks without pre-allocating all work. Never fan out network I/O with bare `gather` over an unbounded or large list.

25. **Rate limiting is per `(tenant_id, host)`, not per source or per job.** The Lua script in `agents/scout/rate_limit.py` is the single authoritative token-bucket implementation. Key pattern: `scout:rl:{tenant_id}:{host}`. `HostRateLimitedError` propagates as a failed `SourceOutcome` — the source is NOT retried in the same job; the retry topology (attempt → tiered topics) handles future jobs.

26. **Sitemap sub-URL discovery does NOT update `Source.last_fetch_status`.** Only the parent sitemap source (the one registered in Mongo) gets a status write. `update_source_status=False` on all `_FetchTask` items enqueued from discovered URLs.

27. **`EmbeddingProvider` and `ChatProvider` are two separate Protocols.** Never combine them into a single `LLMProvider` Protocol. Reason: embedding models and chat models have different capabilities, billing, and token limits; conflating them forces every implementation to satisfy both contracts even when they only need one. `domain/llm/providers.py` is the canonical location.

28. **Retry wrapping goes around the Protocol, not inside the implementation.** `RetryingEmbeddingProvider` wraps any `EmbeddingProvider` and only catches `LLMTransientError` (parent of `LLMRateLimitError`/`LLMTimeoutError`). Implementations map SDK-specific exceptions to the domain hierarchy in `_map_*_error(exc)`. This means retry policy is configurable per-provider without modifying the provider class.

29. **ChromaDB collections are per-tenant per-embedding-model.** Naming: `f"chunks__{tenant_id[:8].lower()}__{model_id.replace('.','_').replace('-','_')}"`. This isolates tenants and allows model migrations (old and new embeddings coexist). Changing this naming scheme requires a data migration.

30. **Chunk text lives in Mongo; vectors live in ChromaDB; they cross-reference via `Chunk.vector_id`.** Never store embeddings in Mongo and never store chunk text in ChromaDB metadata (metadata has a 64KB doc limit and isn't indexed for BM25). Phase 8 hybrid retrieval relies on this separation: BM25 queries Mongo text index, dense queries ChromaDB, RRF merges the two result sets.

31. **Parent-child chunking: parent chunks are stored in Mongo but NOT embedded.** Only child chunks get `vector_id`. Parent text is fetched at query time via `parent_chunk_id` to widen context for the LLM. `RawChunk.is_parent` is True when `parent_index is None`. `KnowledgePipeline` only upserts vectors for child chunks.

32. **`KnowledgePipeline.process_document` is idempotent per `(tenant_id, document_id, embedding_model)`.** The check calls `chunk_repo.list_by_document(tenant_id, doc_id, embedding_model=self._embed.model_id)`, which filters to child chunks under the current model only. If any exist → `KnowledgeResult(skipped=True)`. If the provider changes, `list_by_document` returns empty (old chunks have a different `embedding_model`), and the document is re-chunked and re-embedded; old chunks become orphans and TTL out. This is separate from the worker-level idempotency key (`knowledge:{job_id}`) — that guards against duplicate Kafka deliveries; the pipeline-level check guards against model drift.

33. **`onnxruntime` pinned to `<1.20` in the `rag` dep group.** Required for Intel Mac wheel availability (`onnxruntime==1.26.x` has no `x86_64` macOS wheel). Do not remove this pin without verifying wheel availability for the target platform.

34. **Score combination across retrieval backends uses Reciprocal Rank Fusion (RRF), never raw arithmetic.** `services/retrieval/rrf.py` is the single authoritative implementation; `k=60` is the canonical smoothing constant. Score scales vary wildly (BM25 corpus-dependent, cosine [0,2], reranker [0,1]) and normalising across them is fragile. RRF is rank-only — it does not care about magnitude, only ordering. Never weighted-sum normalised scores; never invent a new fusion algorithm. The Analyst's overall pipeline relies on RRF correctness — if you change `k`, update the golden-example tests too.

35. **Filter by `category_id` AND `tenant_id` at every retrieval stage.** Both `MongoBM25Retriever` (via `_tenant_query` + `$text` filter dict) and `ChromaVectorRetriever` (via `$and` filter on Chroma metadata) MUST include both fields in every query. The vector store metadata is set at upsert time by `KnowledgePipeline`; the retrievers enforce read-side. Cross-category bleed is a tenant-trust violation, not just a quality regression — never relax this filter for "broader recall."

36. **`EmbeddingProvider.embed_batch` accepts `task_type: Literal["document", "query"]`.** Default `"document"` for backward compatibility with all existing call sites. Gemini honors it (`RETRIEVAL_DOCUMENT` vs `RETRIEVAL_QUERY`); OpenAI/Ollama accept and ignore. The vector retriever passes `task_type="query"` at search time so asymmetric embedders use the right model weights. When adding a new embedding provider, you MUST accept the kwarg even if you ignore it — otherwise the Protocol isn't satisfied structurally.

37. **Analyst, validator, and query-expansion prompts live in `services/analysis/prompts/*.md`, loaded via `importlib.resources`.** NEVER embed prompts as Python string literals — prompts are content, not code. They get tuned iteratively without code review overhead. The prompt files MUST reference the exact tool names (`record_analysis`, `record_validation`) that the services pin in their tool schemas. Smoke tests (`tests/unit/test_analysis_prompts.py`) lock the file/tool/rubric-dimension contracts; do not weaken these tests to lock specific prose.

38. **Anthropic chat applies `cache_control: {"type": "ephemeral"}` to system messages by default.** `AnthropicChatProvider(cache_system_prompt=True)` extracts the system message, wraps it in the list-of-blocks format with cache_control, and passes it via the API's `system=` parameter (separate from `messages=`). The Analyst's long persona prompt + the Validator's rubric prompt both qualify — second-call latency drops materially. Retrieved chunks live in the user message and are NOT cached (they change every request). Set `cache_system_prompt=False` only if you have a measured reason; this is a performance and cost win.

39. **Structured LLM outputs use provider tool-use via `complete_with_tools()`, never prose JSON parsing.** All three chat providers satisfy the `StructuredChatProvider` Protocol and accept Anthropic-style tool definitions (`name`, `description`, `input_schema`) — each provider adapts internally. `tool_choice=<tool_name>` forces the model to call that specific tool, eliminating the "did it return JSON?" ambiguity. If a provider returns no `tool_use`/`function_call` block, raise `LLMSchemaError` — do NOT fall back to parsing message content for JSON. The `record_analysis` and `record_validation` tool schemas are the wire contract; if you add a new structured task, define a new tool schema, not a "respond with JSON" prompt.

40. **The Analyst defensively filters hallucinated chunk references.** `_build_analysis_from_tool_args` (in `services/analysis/analyst.py`) drops `supporting_chunk_ids` not in the retrieved corpus, drops insights left with zero valid IDs after filtering, drops citations whose `chunk_id` isn't in the corpus, and truncates excerpts to 500 chars. The prompt rules + tool schema are the first line of defense; this filter is defense-in-depth. The validator's grounding dimension (weight 0.30) is the third layer. Together they make "Analysis claims a chunk that doesn't exist" structurally impossible from the system's perspective even if the LLM tries.

41. **Refinement loops are SEPARATE Kafka work items, not orchestrator-internal iterations.** The analyst worker's idempotency key includes `refinement_loop` (`f"analyst:{job_id}:{refinement_loop}"`) so different loops are distinct events. The orchestrator's `_handle_analysis_completed` republishes `AnalysisPendingEvent(refinement_loop+1, refinement_notes=<persisted validator notes>)` directly via Kafka; it does NOT invoke `refine_node` in the graph. The graph stays paused at `NODE_ANALYZE` between loops. After `refinement_loop >= max_refinement_loops`, the orchestrator publishes the highest-attempt analysis with low confidence rather than failing — graceful degradation over hard failure.

42. **`StructuredChatProvider` is a separate Protocol from `ChatProvider`.** Both live in `domain/llm/providers.py`. `ChatProvider` has `complete` + `stream`; `StructuredChatProvider` has `model_id` + `complete_with_tools`. Concrete providers (`AnthropicChatProvider`, `GeminiChatProvider`, `OpenAIChatProvider`) satisfy both. Services that ONLY need free-form completion type their dependency as `ChatProvider`; services that need tool-use type as `StructuredChatProvider`. Never widen a service that needs tool-use to `ChatProvider` — you'll lose type-safety on the structured call path.

### Naming conventions

- Modules: `snake_case`, descriptive (`category_repository.py`, not `cat_repo.py`).
- Classes: `PascalCase`; concrete Mongo repos prefixed `Mongo` (e.g. `MongoCategoryRepository`).
- Protocols: just the domain name (`CategoryRepository`, no `I` prefix or `Protocol` suffix — PEP 544 structural typing).
- Event class names: `XxxEvent` ending; topic names follow `domain.action.vN` (e.g. `trendstorm.jobs.requested.v1`).
- Test files: `test_<thing>.py`; markers `unit` (no I/O) and `integration` (needs `make up`).
- ENV vars: nested via `__` (`MONGO__URI`, `LLM__ANTHROPIC_API_KEY`).

### Mongo query patterns

- Always `_tenant_query(tenant_id, ...)`.
- Pagination: ULID cursor (`_id < $lt`), never offset.
- Atomic updates: `find_one_and_update` with `return_document=True`.
- Bulk inserts: `ordered=False` (one duplicate doesn't abort the batch).
- Aggregations: `$match` first (must hit an index — verify with `.explain()`). Use `$merge` to materialize rollups for heavy dashboards (Phase 11).

### Test discipline

- **Unit tests** (`-m unit`): pure functions, no Docker. Includes state machine, URL canonicalization, index-registry invariants, edge routing, Pydantic validators, the orchestrator graph with `stream_mode="values"` (no checkpointer).
- **Integration tests** (`-m integration`): require `make up`. Use motor/aiokafka against real services. Async tests use `pytest-asyncio` auto mode.
- **Always assert tenant isolation**: a fixture that inserts under tenant A must verify tenant B sees nothing.

### Build/dev workflow

- `uv sync --all-groups` to install everything; `uv sync --no-dev` for prod images.
- `make check-all` = lint (ruff) + typecheck (mypy strict) + unit tests. Run before committing.
- `make run-dev` (API) and `make run-worker-dev` (orchestrator) run from host with console logs and DEBUG level, reading `.env` THEN `.env.local` (the latter overrides for localhost-published Docker ports).
- `.env.local` is gitignored; commit `.env.local.example` only.
- Dockerfiles are multi-stage: builder uses `uv sync --frozen --no-dev [+groups]`, runtime is minimal `python:3.12-slim` with non-root user `trendstorm` (uid 1000), `tini` as PID 1, `PYTHONUNBUFFERED=1`.

### Things to NEVER do

- Add a query without `tenant_id` (unless the collection is genuinely tenant-less like `idempotency`).
- Use string literals for collection names.
- Use `auto.create.topics=true` on Kafka.
- Use `enable_auto_commit=True` on Kafka consumers (silent message loss).
- Store vectors in Mongo or raw HTML in Mongo.
- Use `asyncio.gather` over an unbounded fan-out of network calls without a Semaphore.
- Use `datetime.utcnow()` (naive datetime).
- Use module-level mutable singletons (clients live on `app.state`, accessed via DI).
- Use the deprecated `@app.on_event("startup")` (use lifespan context manager).
- Use `time.time()` for measuring durations (use `time.perf_counter()`).
- Catch `Exception:` without narrowing or re-raising — domain errors must propagate as their concrete types so the API error handler can map them.

### Things to ALWAYS do

- New repository → also add tests asserting tenant isolation + unique-constraint behavior.
- New collection → add to `Collection` enum AND add at least one index (test asserts every collection has indexes).
- New event type → add to `AnyEvent` discriminated union with its `event_type` Literal.
- New stage → update `Stage` enum AND `_TRANSITIONS` table AND retry-budget map AND `_STAGE_TO_STATUS` mapping.
- New domain entity → create `models.py` + `repository.py` (Protocol) + concrete `Mongo<Name>Repository` + add to `repositories/__init__.py`.
- New router → wire in `api/main.py:create_app`, add `Depends(require_tenant)` at router level.
- New Kafka topic → create explicitly in `kafka-init` (compose) AND add to `Topic` enum.
- When a domain model field is only populated post-creation (e.g. `Chunk.updated_at` written by `set_vector_id`, `Chunk.vector_id` written after Chroma upsert), declare it `Optional` with `default=None`. Otherwise `extra="forbid"` causes reads to fail after any `$set` update adds the field to the Mongo document.

43. **`publish_node` (and all handoff nodes) are dual-mode.** Check `config.get("configurable", {}).get("kafka_producer")`. If `None`, execute stub (unit tests pass without Docker). If present, publish the pending event and return partial state. This pattern applies to ALL nodes that delegate to external workers (ingest/embed/analyze/publish). Never remove the stub path — it is load-bearing for the unit test suite.

44. **SSE events are ephemeral UX signals.** `SSECoordinatorWorker` routes ALL failures to DLQ immediately rather than the tiered retry topics. Retry delay makes stale stream events worse, not better. The Redis Streams log provides durable replay; Pub/Sub is fire-and-forget for live clients.

45. **`RedisStreamStore` and `RedisPubSub` do NOT own a Redis client.** Both classes accept the live redis-py async client via `.init(redis_client)` — called after `RedisClient.connect()` in each worker's `run_worker()`. Lifecycle management stays with `RedisClient`. Pattern mirrors how `MongoCheckpointer` keeps a separate pymongo connection from the shared `MongoClient`.

46. **SSE generator uses subscribe-before-read.** `sse_event_generator()` calls `pubsub.subscribe()` BEFORE calling `stream_store.read_from()` for history replay. This prevents the race: events published between XRANGE and SUBSCRIBE are buffered in the Pub/Sub channel, then de-duplicated by `seen_seqs`. Never reorder to read-before-subscribe; doing so silently drops events published in the gap.

47. **SSE seq numbers are job-scoped, monotonically assigned by the SSE Coordinator via `INCR`.** The coordinator is the single writer to Redis; all workers that want to emit stream events publish `StreamPartialEvent` to `stream.partial.v1` Kafka topic. The coordinator assigns seq atomically. This means: (a) seq is globally ordered per job even if multiple workers emit events concurrently; (b) `Last-Event-ID` resumption is reliable; (c) no worker directly writes Redis — all SSE writes go through the coordinator.

48. **Publisher service PDF rendering is best-effort.** `PublisherService.publish()` catches ALL exceptions from `render_pdf()` and logs a warning rather than failing the job. On macOS dev, weasyprint fails without GTK/Pango. In production (Docker image includes system libs), PDF succeeds. `PublishResult.pdf_report_id` is `str | None` — callers must handle `None`. `PublishCompletedEvent.pdf_report_id` is also nullable for the same reason.

49. **`emit_stream_event()` is the ONLY place that constructs `StreamPartialEvent` for Kafka.** `services/streaming/emit.py` is the single helper used by all workers (publisher, and future streaming agents). Never inline `StreamPartialEvent` construction + `send_and_wait` calls in worker handler bodies; always delegate to this helper.

50. **All metrics are declared in `shared/metrics/registry.py`. No service imports `prometheus_client` directly.** `_TrendStormMetrics.__init__` is the single registry; `_FORBIDDEN_LABELS` enforces cardinality at import time. `METRICS` is the module-level singleton. Unit tests MUST use `make_test_metrics()` to get an isolated instance — never increment `METRICS` in tests or cross-test pollution will corrupt counters.

51. **High-cardinality identifiers (job_id, document_id, chunk_id, correlation_id, source_id, analysis_id) MUST NOT appear as Prometheus label values.** They belong in trace span attributes (`Attr.*` constants from `shared/tracing/semantics.py`) and log fields. `_FORBIDDEN_LABELS` enforces this at import time. Grafana queries that need to drill down by job_id use Jaeger/Loki (cross-linked from the dashboard) — not Prometheus.

52. **`Attr.*` constants from `shared/tracing/semantics.py` are the ONLY allowed span attribute key strings.** Never use raw `"trendstorm.job_id"` or similar string literals in span `attributes={}` dicts. If a new attribute is needed, add it to `Attr` first (and update `test_business_spans.py` to assert it if it's referenced in test assertions).

53. **`BaseConsumer._record_handle_metrics(event, status, elapsed)` is the per-worker metrics hook.** Override it in any worker that has a per-event Prometheus metric pair (duration histogram + count counter). Never add a separate timer inside `handle()` — the base class already measures elapsed time and passes it to the hook. Metric recording errors in the hook MUST be swallowed silently (`try/except Exception: pass`) — metric failure must never crash business logic.

54. **Alert runbooks live in `ops/runbooks/*.md`.** Every Prometheus alert rule MUST have a `runbook:` annotation pointing to the corresponding file. Runbook URIs use the GitHub raw path convention (`https://github.com/…/ops/runbooks/<name>.md`). When adding a new alert, add the runbook first. A fired alert with no runbook is worse than no alert.

55. **OTel tail sampling is configured in `docker/config/otel-collector.yaml`.** Three policies (in order): status=ERROR always samples; latency>120s always samples; probabilistic 5% for everything else. `decision_wait: 30s` covers slow analyst worker spans. The tail_sampling processor MUST come before `batch` in the pipeline so sampling decisions precede export. Never move tail_sampling after batch — it would make decisions on incomplete trace data.

56. **`complete_with_tools()` returns a 3-tuple `(tool_name, args, TokenUsage)`, NOT a 2-tuple.** `TokenUsage` (frozen: `input_tokens`, `output_tokens`, `cached_tokens`) is defined in `domain/llm/models.py`. All three chat providers (Anthropic, Gemini, OpenAI) return real token counts. Code that calls `complete_with_tools` MUST unpack 3 values. Adding a new chat provider requires returning a `TokenUsage` — never return a 2-tuple.

57. **Eval panel uses `asyncio.gather(return_exceptions=True)` — one judge failure must NOT abort the panel.** `LLMPanel.vote()` collects all non-exception results, then checks `n_valid >= min_quorum`. Only after the quorum check does it raise `PanelInsufficientVotesError`. Never use sequential judge calls or `return_exceptions=False` — that collapses the panel to the weakest judge's availability.

58. **Citation accuracy evaluation is deterministic (no LLM judge).** `CitationLookupEvaluator` uses embedding cosine similarity between the citation excerpt and the chunk text. Threshold: 0.65 cosine. When a `GoldenExample` is provided, the evaluator looks up chunks from `example.chunks` first (in-memory, no I/O). When `chunk_repo` is None and no example, citations score 0.0. Never add an LLM judge to citation accuracy — it is a lookup problem, not an inference problem.

59. **Golden examples live in `eval/golden/` in git. Git is the source of truth; LangSmith is the UI.** JSON files serialize to `GoldenExample` Pydantic models. `chunk_id` values in golden examples must match `supporting_chunk_ids` in the `ExpectedAnalysis`. `required=True` on an `ExpectedInsight` means a passing analysis MUST surface it; `required=False` means optional. Validate with `GoldenExample.model_validate(json.load(...))` before committing. Never store golden examples in Mongo or LangSmith — they are reviewed like code.

60. **`langsmith.Client` SDK only — NEVER `langchain`.** `infrastructure/langsmith/client.py` wraps `langsmith` (the observability SDK). `langchain` is a prompt-engineering framework we do not use. Deferred import `import langsmith` in `connect()` so the SDK is only loaded when the API key is set. All write methods (`push_eval_results`) are best-effort — failures are logged as warnings, never raised.

61. **1% production eval sampling is deterministic per `job_id`.** `hash(job_id) % 100 == 0` is the sampling formula in `AnalystWorker._run_pass()`. Same `job_id` always samples or always skips, regardless of retries or worker restarts. `EvalSampleEvent` publish is best-effort — exception in the sampling block MUST be caught and logged, never propagated to the business logic path. Idempotency key for the eval worker: `f"prod_eval:{job_id}:{analysis_id}"` — scoped to both to allow separate evaluation of refinement-loop analyses.

62. **`GoldenCoverageEvaluator` raises `ValueError` on production samples (no golden expected).** This is the intended API — it is NOT an error. `EvalRunner._evaluate_example` catches `ValueError` and silently skips the evaluator (dimension excluded from this example's aggregation). `ProductionEvalPipeline._run_evaluators` does the same. Never convert this `ValueError` to a score — an absent golden means COVERAGE cannot be measured, not that it scored zero.

63. **Job creation via outbox ONLY — direct Kafka publish from `JobService` is FORBIDDEN.** `JobService.create_job` writes job + `OutboxEntry` inside a single Mongo transaction (`start_session` + `start_transaction`). The `OutboxRelayWorker` polls and publishes to Kafka. This closes the atomicity window: if Kafka is down when a job is created, the outbox accumulates entries and drains on recovery. A direct `KafkaProducer.publish` in `JobService` would leave jobs stuck in `PENDING` on Kafka failure.

64. **API keys are stored as SHA-256 hashes only. Plaintext is returned once on creation and never stored.** `ApiKey.key_hash` in Mongo is `sha256(plaintext_key)` hex. `ApiKey.key_prefix` (first 8 chars of the random portion) is stored for display. The `AuthService.create_key` method returns `(ApiKey, raw_key)` — the router must surface `raw_key` to the user immediately in the 201 response. `raw_key` is never logged, never stored, never returned again.

65. **`AUTH_MODE=disabled` is forbidden in production (`APP__ENV=prod`).** `AuthMiddleware.__init__` raises `RuntimeError` if `settings.mode == AuthMode.DISABLED and app_env == Environment.PROD`. This is checked at app startup, not per-request, so the failure is loud and immediate rather than silent.

66. **`RateLimitMiddleware` and `AuthMiddleware` read lifespan-built clients lazily from `request.app.state`.** Neither reads Redis/Mongo in `__init__` — those clients are connected in lifespan, after middleware is registered. `RateLimitMiddleware` initializes `self._bucket` on first request via `getattr(request.app.state, "redis", None)`. `AuthMiddleware` reads `auth_service` via `getattr(request.app.state, "auth_service", None)`. If either is `None`, the middleware falls back safely (rate-limit: skip; auth: let the mode-specific logic decide). This is the standard Starlette pattern for lifespan-dependent services.

67. **`QuotaService` is advisory, not transactional.** Two simultaneous `create_job` calls can both pass the quota check before either job is counted in the ledger. The quota check is a best-effort advisory gate — the cost ledger + billing alert is the hard backstop. Accepting this race is correct: a distributed lock on job creation for quota enforcement would create a single point of failure and add latency. See `ops/runbooks/cost-overrun.md`.

68. **`record_llm_cost` persists to the cost ledger via fire-and-forget.** Callers pass `job_id` + `ledger` to `record_llm_cost`; the function schedules `loop.create_task(_write())` if a running event loop exists. Ledger write failure is caught and logged — NEVER propagated. Prometheus metrics are always updated regardless of ledger write success. The ledger is for billing reconciliation; Prometheus is for operational alerting.

69. **`tenants` and `api_keys` collections are exempt from the "unique indexes must start with `tenant_id`" rule.** `tenants` IS the root entity (no outer `tenant_id` to scope by). `api_keys__key_hash_unique` is global because key lookup happens before we know the tenant. Both are listed in `GLOBAL_OK` in `tests/unit/test_indexes.py`. No other collection should be added to `GLOBAL_OK` without explicit discussion.

70. **`BusinessRuleError(code="quota_exceeded")` → HTTP 402.** The error handler in `api/error_handlers.py` checks `exc.code` specifically. Other `BusinessRuleError` codes map to 400. When adding new business rule errors that should return specific HTTP codes, extend the handler's `elif isinstance(exc, BusinessRuleError)` branch rather than creating new exception subclasses.

71. **Helm workers that are single-writer use `strategy: type: Recreate`.** The outbox-relay-worker is the canonical example — running two relay instances concurrently doesn't cause data corruption (outbox is idempotent via `mark_published`), but it does cause double-publishing. `Recreate` ensures the old pod terminates before the new one starts. Apply the same strategy to any future single-writer worker.

72. **Argo Rollouts `AnalysisTemplate` gates on HTTP 5xx error rate < 1%.** The canary analysis queries Prometheus every 30s; fails if 3 consecutive readings are below 99% success. This is the minimum viable gate for production safety. Add latency p99 as a second metric when baseline latency data is available.

73. **NetworkPolicies use allowlist (default-deny) posture.** Both `default-deny-ingress` and `default-deny-egress` policies apply to all pods in the `trendstorm` namespace. Every traffic flow is explicitly permitted in `k8s/network-policies.yaml`. When adding a new worker or external service, add the corresponding egress policy before deploying.

### Phase 13 — Security hardening conventions

74. **`validate_url(url, *, resolved_addrs=None) -> ValidatedURL` is the ONLY function that validates URLs for SSRF.** Located at `infrastructure/security/ssrf.py`. `resolved_addrs` is an injectable override for unit tests (no DNS I/O in tests). Production code calls this from `asyncio.get_event_loop().run_in_executor(None, validate_url, url)` because socket.getaddrinfo is blocking. Never bypass this function for any outbound HTTP fetch.

75. **Every redirect hop is validated separately via `validate_redirect(from_url, to_url)`.** The Scout fetcher (`agents/scout/fetcher.py`) follows redirects manually with `follow_redirects=False`. Max 3 redirects (hard constant `MAX_REDIRECTS` in `ssrf.py`, not from config). Scheme downgrade (https→http) is blocked on any hop. On block: increment `trendstorm_security_block_total`, write to `audit_log`, raise `SSRFBlockedError` (a `FetchError` subclass).

76. **`SSRFBlockedError.reason` MUST match a `SecurityBlockReason` enum value.** `SecurityBlockReason` is defined in `shared/metrics/registry.py`. The reason is used as a Prometheus label (`trendstorm_security_block_total{reason=...}`). Never pass a raw string that isn't in the enum — the label cardinality is bounded by the enum.

77. **Security block metrics use `tenant_id_hash`, never raw `tenant_id`.** Call `record_security_block(reason, tenant_id)` from `shared/metrics/registry.py`. It computes `tenant_id_hash = f"b{hash(tenant_id) % 100:02d}"` internally (100 bounded buckets). Never call `METRICS.security_blocks.labels(tenant_id=...)` directly — `tenant_id` is in `_FORBIDDEN_LABELS`.

78. **`audit_log` collection writes are fire-and-forget (fail-open).** `MongoAuditLogRepository.append()` catches all exceptions and logs a warning rather than raising. A transient Mongo error must never abort an SSRF check or PII detection path. The audit log is observability infrastructure, not a hard dependency.

79. **`AuditLogEntry` has `extra="forbid"` and is `frozen=True`.** It is never mutated after creation. TTL is 365 days (regulatory minimum). `event_type` values used in the codebase: `"ssrf_blocked"`, `"url_blocked"`, `"pii_detected"`. Add new event types here as they are introduced.

80. **Chunk content is wrapped in `<chunk id="..." source="...">` tags** before injection into the analyst user message (`services/analysis/analyst.py:_format_user_message`). The analyst system prompt (`analyst_system.md`) contains the CRITICAL SECURITY RULE section that explicitly forbids following instructions embedded in chunk content. Never remove this section or the `<chunk>` delimiters — they are the primary prompt injection defense.

81. **The validator rubric has 6 dimensions (not 5) as of Phase 13.** Weights: grounding=0.28, faithfulness=0.23, quality=0.18, coverage=0.13, specificity=0.08, injection_resistance=0.10. Sum=1.00. `injection_resistance` score of 0.0 forces `passed=false` regardless of other scores. Do not remove or reweight this dimension without updating both `validator_system.md` and all golden examples that test for it.

82. **PII detection runs via `DefaultPIIDetector.detect_and_redact(text)` before chunk text is sent to external LLMs.** The detector is Protocol-typed (`PIIDetector` in `infrastructure/security/pii.py`) for future Presidio integration. On detection: redact in-place (`[REDACTED:SSN]` etc.), write to `audit_log`, call `record_security_block(pii_type, tenant_id)`. The `detect_and_redact` method is pure (no I/O) — all side effects are the caller's responsibility.

83. **Global URL blocklist is loaded from `ops/security/global-blocklist.txt` at module import** in `infrastructure/security/blocklist.py`. It is a frozenset — zero per-request I/O. Per-tenant blocklist entries live in MongoDB `url_blocklists` collection and are cached in-process for 60 s per tenant via `_TENANT_CACHE`. Cache miss triggers a `list_for_tenant` query. Cache does NOT invalidate across worker processes — new blocklist entries take up to 60 s + any number of running workers to propagate.

84. **`sanitize_text(text)` MUST be called before persisting analysis text** and before rendering HTML/PDF reports. Located at `infrastructure/security/sanitize.py`. Strips `<script>`, `<style>`, `<iframe>`, `<object>`, `on*=` event handlers, `javascript:` URIs, and `data:` URIs. Use `html_escape(text)` for raw text interpolated into HTML attributes or nodes in report templates.

85. **Adversarial golden examples live in `eval/golden/adversarial/`.** Each JSON file follows the same `GoldenExample` schema as other goldens, with additional fields: `expected_analysis.forbidden_phrases` (list of strings that must NOT appear in any output), `expected_analysis.injection_resistance_min_score`, `expected_analysis.adversarial_chunk_id`. Eval runner must check forbidden phrases and the injection_resistance dimension score.

### Phase 13.5 — HITL review queue conventions

86. **`review_gate_node` is dual-mode.** Check `config.get("configurable", {}).get("kafka_producer")`. If `None` (unit tests), always return `{stage: PUBLISHING}`. If present, load `TenantSettings`, evaluate flagging criteria, and either pass through or create a `ReviewRequest` + publish `ReviewRequestedEvent`. Never remove the stub path — it is load-bearing for the unit test suite (same pattern as all other handoff nodes, rule 43).

87. **`skip_hitl_gate=True` bypasses `review_gate_node` entirely.** The orchestrator sets this flag when injecting state on any review resolution (approve, reject, or refinement). The node checks it FIRST — before loading settings or Kafka. It resets the flag to `False` on the returned state update so future pipeline cycles gate normally. This prevents re-gating after refinement loops.

88. **`AWAITING_REVIEW → FAILED` is NOT a valid transition.** The only terminal outcomes from `AWAITING_REVIEW` are `PUBLISHING` (approved), `ANALYZING` (refinement requested), `REJECTED` (rejected or timed out), and `CANCELLED`. If a system error occurs during review, it surfaces via `REJECTED` (timeout sweeper) — not `FAILED`. This is by design: `FAILED` means the system broke; `REJECTED` means a human (or timeout) made a decision.

89. **`Stage.REJECTED` is a terminal stage distinct from `Stage.FAILED`.** `REJECTED` means the analysis was reviewed and declined; `FAILED` means a pipeline error. Both have empty successor sets. The `is_terminal` property covers both. `JobStatus.REJECTED` maps to HTTP 200 (job returned, status is informational); do not treat it as an error response.

90. **Review resolve goes through the outbox, not direct Kafka.** `POST /v1/reviews/{id}/resolve` writes a `ReviewRequest` update + `OutboxEntry` in a single Mongo `start_transaction()` session. The `OutboxRelayWorker` then publishes `ReviewResolvedEvent` to Kafka. This is the same pattern as `JobService.create_job`. Never call `KafkaProducerClient.send_and_wait` directly from an API handler — that breaks the atomicity guarantee if Kafka is down at resolve time.

91. **`list_expired_pending()` is the only cross-tenant Mongo query in the system.** `MongoReviewRepository.list_expired_pending()` intentionally bypasses `_tenant_query()` — the sweeper must scan all tenants for expired reviews. This is the documented exception to Rule 3. All other `MongoReviewRepository` methods go through `_tenant_query()`.

92. **The timeout sweeper deploys as single replica (`strategy: Recreate`).** Two sweeper replicas don't cause data corruption (the `mark_timed_out` findOneAndUpdate is atomic), but they do cause double-publishing to Kafka and double SSE events. `Recreate` prevents this. The `PendingReviewsAgingHigh` alert covers the sweeper being down (fires at 80% of SLA = 38.4h before auto-reject).

93. **`require_role(role)` is a FastAPI `Depends` factory, not a decorator.** It returns `Depends(_check)` where `_check` reads `request.state.auth_context.roles` (populated by `AuthMiddleware`). The `reviewer` role is required on the `/v1/reviews` router. Roles are stored on `ApiKey.roles: list[str]` (default `[]` — backward-compatible). JWT authentication reads roles from the `roles` claim (string or list).

94. **`trendstorm_reviews_pending_oldest_created_at` (Gauge) is the aging signal for alerting.** Workers that create or resolve `ReviewRequest` records must keep this gauge updated: `METRICS.reviews_pending_oldest_created_at.labels(tenant_id_hash=...).set(oldest_review.created_at.timestamp())`. When all pending reviews resolve, set to 0. The `PendingReviewsAgingHigh` alert computes `(time() - gauge) / 3600 > 38.4` — wrong or stale gauge values produce false positives/negatives.

95. **`TenantSettings` is optional — absent row means `DEFAULT_TENANT_SETTINGS` (HITL off).** `MongoTenantSettingsRepository.get_for_tenant` returns `None` for tenants without a settings row. All callers must handle `None → DEFAULT_TENANT_SETTINGS`. Existing tenants without a row behave as `hitl_mode=OFF` — no behavior change unless a row is explicitly created. This is the fail-open design: new infrastructure does not force HITL on tenants who haven't opted in.

### Phase 14 — SDK conventions

96. **`trendstorm-shared` is the single source of truth for the API wire format.** Enums (`JobStatus`, `SourceType`, `ReportFormat`, `ReviewStatus`, `ReviewDecision`, `StreamEventType`) and request/response models live in `packages/trendstorm-shared/src/trendstorm_shared/`. When adding a new API field: add it to the shared model first, then update the server router. The server does NOT import from `trendstorm-shared` at runtime (no circular dep); the shared package is consumed by the SDK and by cross-package wire-format tests.

97. **SDK models use `extra="ignore"`, server models use `extra="forbid"`.** The server validates requests strictly (extra fields = bug). The SDK parses responses permissively so an older SDK version doesn't break when the server adds an optional field. This is the opposite convention and is intentional — never flip them.

98. **The SDK Python import is `trendstorm_sdk`; the PyPI name is `trendstorm`.** The server's Python package is `trendstorm` (at `src/trendstorm/`). In the monorepo both coexist because they're in different directories, but `pip install trendstorm` installs the SDK. The server is never published to PyPI. Do not rename the SDK import namespace.

99. **`retry_request()` in `sdk/python/src/trendstorm_sdk/_retry.py` is a function, not a transport subclass.** It wraps a zero-argument coroutine so SSE streaming bypasses retry logic entirely. A transport-level retry would intercept streaming responses (status 200 with `text/event-stream`) and attempt to buffer/retry them, which corrupts the event stream. Never move retry to the transport layer.

100. **SDK integration tests run only when `TRENDSTORM_API_KEY` is set; otherwise they skip.** `sdk/python/tests/integration/conftest.py` calls `pytest.skip()` if the env var is absent. Integration tests are not part of `make test` (server unit suite) — run via `make sdk-test-integration`. Staging-only tests are marked `@staging` and run only on main-branch CI.

### Phase 15a — Dashboard conventions

101. **Dashboard is a static SPA, not a service.** `web/dashboard/` produces a `dist/` artifact served by nginx (Helm chart `helm/dashboard/`) or S3+CloudFront. It does NOT appear in the 10-service count and does NOT get a Kafka consumer group or Mongo connection. Never add server-side logic to the dashboard — all data fetching is client-side via the REST API.

102. **No business logic in React components. Use TanStack Query + custom hooks.** Components render data from query results. Query options factories live in `src/api/queries/<resource>.ts` and are the only place that calls `api.get/post`. Custom hooks in `src/hooks/` encapsulate stateful side effects (SSE connection, tenant selection). Never put `fetch()` calls directly in component bodies.

103. **Type generation is committed and gated in CI.** `src/api/types.generated.ts` is the committed baseline from `npm run codegen` (which calls `openapi-typescript` against `/v1/openapi.json`). The CI job `codegen-check` re-runs codegen on every `main` push and fails if the committed file diverges. When adding a new API field: update the server, run `npm run codegen` in `web/dashboard/`, commit both together.

104. **Auth0 roles come from `https://trendstorm.ai/roles` JWT claim, NOT from an API endpoint.** The `RoleGuard` and `useRoles()` hook read this claim from the Auth0 user object. Roles are injected by an Auth0 Action in the Post Login flow. Never create an `/api/me` endpoint for role-checking — that bypasses the Auth0 token and breaks the trust model.

105. **SSE uses a custom fetch-based `SSEConnection` class, never `EventSource`.** Located at `src/lib/sse.ts`. Reason: `EventSource` does not support custom request headers (`Authorization:`, `X-Tenant-ID`). The `useSSE` hook (`src/hooks/useSSE.ts`) wraps `SSEConnection`, handles token refresh, persists `Last-Event-ID` to `sessionStorage`, and stops the connection on terminal events. Never use native `EventSource` for authenticated streams.

106. **`config.json` is the source of truth at runtime; `VITE_*` env vars are dev fallbacks only.** `src/api/client.ts:loadConfig()` fetches `/config.json` first. In Kubernetes, this file is mounted from a ConfigMap (so the same Docker image runs in staging and prod). In local dev it 404s, and `loadConfig()` falls back to `import.meta.env.VITE_*`. Never bake Auth0 client IDs into the bundle — they belong in `config.json`.

107. **Dashboard unit tests mock Auth0 globally in `tests/setup.ts`.** The mock injects a test user with `reviewer` and `admin` roles and a single tenant. Component tests never need a real Auth0 provider or HTTP server. E2E Playwright tests skip (via `test.skip()`) when `PLAYWRIGHT_AUTH_TOKEN` is absent — they only run against a live staging environment with a pre-seeded test user.

108. **Tailwind design tokens are CSS custom properties in `src/index.css`, not hardcoded colors.** All `bg-*`, `text-*`, `border-*` classes in components use semantic names (`bg-primary`, `text-muted-foreground`). The CSS variables are defined in `:root`. Never use `bg-blue-600` etc. directly in component code — use semantic Tailwind classes so dark mode works by toggling the `.dark` class on `<html>`.

---

## 5. Maintaining This Document (READ EVERY SESSION)

**This file is a living document.** Treat it as code. Out-of-date context produces worse output than no context.

### When to update

At the **end of every meaningful work unit** — phase completion, sub-phase milestone, significant refactor, new dependency, new infrastructure component, new architectural decision, or any fix that contradicts something already in this file. If you finished a unit of work and didn't update this file, the work isn't done.

### How to update

1. **Section 2 — Current Progress & State.** When a phase is fully done with tests passing, change `🚧` to `✅` and write a 1–2 paragraph summary in the same dense style as the existing phase summaries: what was built, what the non-obvious decisions were, what works end-to-end now. Mirror the existing structure (file paths, key class names, the "non-trivial choices" pattern). Don't write a changelog — write a state-of-the-system snapshot.

2. **Section 3 — Next Steps & Pending Tasks.** Remove the phase that just completed. Move whatever's now next to the top. Add any pending polish items the just-finished work uncovered. Demote items that turned out to be premature.

3. **Section 4 — Specific Code Patterns & Rules.** Add a rule ONLY when a new architectural decision has been made and ratified (either explicitly with the user, or via a pattern applied consistently across 2+ places). Remove a rule ONLY when explicitly overridden. **Never silently change a rule** — if the user pushes back on one, propose a diff and wait for approval before applying.

4. **Project Overview, tech stack, directory layout.** Update when new top-level modules appear, new services join the deployable units, or a major dependency is swapped.

### Writing-style discipline

- Keep the prose density consistent with the rest of the file. No fluffy preambles, no "in this section we will discuss." Each sentence must add a technical fact a reader couldn't reconstruct from the file tree.
- Use file paths and class/function names liberally — they let future sessions grep instantly.
- Flag tradeoffs and non-obvious choices in parentheses or with a short "why" clause. Future sessions need to know WHY a decision was made, not just what it is.
- Hard rules use "MUST/NEVER" wording in section 4. Recommendations use "should/prefer." Patterns use "when X, do Y."

### What NOT to do

- Don't paste raw code into this file. It's prose-with-pointers, not source.
- Don't list every file ever created. List structurally important ones; the file tree is the inventory.
- Don't track ephemeral state (current branch, last commit, in-progress edits). That's git's job.
- Don't make this file long for its own sake. If a rule is obvious from the codebase (and well-tested), trim it. Density beats length.
- Don't append phase logs at the bottom. Update sections 2 and 3 in place — this is current-state, not history.

### Commit alongside code

Treat changes to `CLAUDE.md` as part of the same commit as the code change that motivated them. The git history of `CLAUDE.md` should track the project's architectural evolution, mirroring section 2's content over time.

### Self-check before ending a session

Before considering work done, ask: "If a fresh Claude Code session opened this repo tomorrow with no context but this file, would it know exactly what to do next?" If the answer is no, update the file first.