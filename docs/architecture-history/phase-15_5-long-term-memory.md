# Phase 15.5 — Long-term Memory: Episodic + Semantic

## Overview

Phase 15.5 adds a persistent memory layer to TrendStorm. After each approved analysis is published, a new `memory_consolidation_worker` extracts durable factual claims (semantic memories) and records a per-job summary (episodic memory). These memories are retrieved at Analyst time and injected into the context alongside retrieved chunks, providing historical continuity across analysis runs within the same category.

## Architecture

### New Stage: MEMORY_CONSOLIDATION

The pipeline now flows:

```
PUBLISHING → MEMORY_CONSOLIDATION → COMPLETED
```

Memory failure is **non-blocking**: if the consolidation worker fails permanently (DLQ), the `after_memory_consolidation` edge routes to `NODE_END` gracefully. The report is already published and visible to the user. Memory enriches future runs but must not gate user-visible completion.

### Storage

- **Mongo `memories` collection**: primary record store. Per-memory document containing content, kind, confidence, tags, tenant_id, category_id, source_job_id. TTL: 730 days (2 years).
- **ChromaDB memory collections**: named `memories__{tenant_id[:8]}_{model_id_safe}`, separate from the chunk vector store. Stores embeddings for cosine-similarity retrieval and supersede detection.
- Cross-reference: `Memory.content_embedding_id` = ChromaDB ID = Mongo `_id` (same ULID).

### Two Memory Kinds

**Episodic memory** (`MemoryKind.EPISODIC`): one record per job. Written by `EpisodicMemoryWriter`. Content is the analysis summary. Idempotent via `exists_for_job()` check — safe on Kafka redelivery.

**Semantic memory** (`MemoryKind.SEMANTIC`): N durable factual claims extracted by a haiku-class LLM call (`record_memories` tool). Confidence threshold: 0.5 minimum. Cap: `MEMORY__MAX_SEMANTIC_MEMORIES_PER_JOB=10` (cost control).

### Supersede Detection

Before writing a new semantic memory, the extractor queries ChromaDB for the most similar existing active semantic memory in the same `(tenant_id, category_id)`. If cosine similarity ≥ 0.92 (`MEMORY__SUPERSEDE_SIMILARITY_THRESHOLD`), the old memory is marked `superseded_by=new_id` and `is_active=False`. The new memory takes its place. No LLM required for the supersede decision — rank-only cosine comparison in the same embedding space is sufficient.

### Analyst Integration

`MemoryRetriever` runs in parallel with `HybridRetriever` inside `Analyst.produce_analysis()`. Retrieved memories are rendered as `<memory id="..." kind="..." confidence="...">` tags in the analyst user message, before the chunk evidence section. The analyst prompt instructs the model:
- Chunks are authoritative for recency.
- Memories provide historical context.
- Disagreements between memories and chunks should surface explicitly (chunks win on recency).

Memory retrieval is best-effort: if ChromaDB has no memories yet (first run for a category), the empty list is passed and the analyst proceeds with chunks only.

### Kafka Topology

| Topic | Partitions | Retention |
|---|---|---|
| `trendstorm.memory.pending.v1` | 6 | 3 days |
| `trendstorm.memory.completed.v1` | 6 | 7 days |
| `trendstorm.retry.memory.30s.v1` | 6 | 1 day |
| `trendstorm.retry.memory.5m.v1` | 6 | 1 day |
| `trendstorm.retry.memory.1h.v1` | 6 | 1 day |

Retry topology: attempt 1 → 30s → 5m → 1h → DLQ.

### Idempotency

- Worker-level: key = `memory:{job_id}`. One memory consolidation request per job.
- Episodic writer: `exists_for_job(tenant_id, job_id)` check before writing.
- Semantic extractor: claims are content-addressed — ChromaDB cosine check handles near-duplicates via supersede.

### User-curated Memories API

`POST/GET/DELETE /v1/categories/{id}/memories` — requires `tenant_admin` role. Lets operators inject known facts before the first analysis run for a new category, or correct stale memories. User-curated memories have `source=USER_CURATED` and are injected alongside auto-extracted ones. The same supersede logic applies (next analysis extraction may supersede user facts if similarity ≥ threshold — intentional, not a bug).

### Backfill

`scripts/backfill_memories.py` publishes `MemoryPendingEvent` for all completed analyses that lack a memory record. Idempotent: checks `exists_for_job` before publishing. Safe to run against a live system. Run with `--dry-run` to preview.

## Non-obvious Design Decisions

**Memory failure is non-blocking by design.** The pipeline completes normally even if the entire consolidation worker is down. This prevents a late-added feature from retroactively gating the user-visible job completion path.

**ChromaDB collection per (tenant, embedding model).** Same naming scheme as chunk collections but prefixed `memories__`. This allows model migrations without cross-contaminating tenant vectors or requiring a full rebuild.

**Separate ChromaDB client per worker.** `ChromaMemoryStore` is a separate class from `ChromaVectorStore`. The analyst worker connects to both concurrently. The memory consolidation worker only uses `ChromaMemoryStore`. This preserves the existing chunk store contract.

**No LLM for supersede.** Supersede detection is purely similarity-based. Adding an LLM judge would add latency and cost for a decision that is fundamentally a distance computation.

**Memory retrieval `top_k` set lower than chunk `final_k`.** Memories augment; chunks are the primary evidence source. Injecting too many memories would overwhelm the chunk context and create a feedback loop where old memories influence new analyses which create more memories.

## Files Added/Modified

### New
- `src/trendstorm/domain/memories/models.py` — `Memory`, `MemoryKind`, `MemorySource`
- `src/trendstorm/domain/memories/repository.py` — `MemoryRepository` Protocol
- `src/trendstorm/infrastructure/mongo/repositories/memory_repository.py` — `MongoMemoryRepository`
- `src/trendstorm/infrastructure/vectors/chroma_memory_store.py` — `ChromaMemoryStore`
- `src/trendstorm/services/memory/episodic_writer.py` — `EpisodicMemoryWriter`
- `src/trendstorm/services/memory/semantic_extractor.py` — `SemanticMemoryExtractor`
- `src/trendstorm/services/memory/retrieval.py` — `MemoryRetriever`, `RetrievedMemory`
- `src/trendstorm/services/memory/prompts/memory_extraction_system.md` — LLM system prompt
- `src/trendstorm/orchestration/workers/memory_consolidation_worker.py` — `MemoryConsolidationWorker`
- `src/trendstorm/api/routers/memories.py` — `/v1/categories/{id}/memories` router
- `docker/memory_consolidation.Dockerfile`
- `scripts/backfill_memories.py`
- `sdk/python/src/trendstorm_sdk/resources/memories.py` — `MemoriesResource`
- `tests/unit/test_memory_models.py`
- `tests/unit/test_memory_services.py`

### Modified
- `src/trendstorm/shared/config/__init__.py` — `MemorySettings` added to `Settings`
- `src/trendstorm/shared/tracing/semantics.py` — 7 new `Attr.*` memory span attributes
- `src/trendstorm/shared/metrics/registry.py` — `MemoryKindLabel`, 4 new metrics
- `src/trendstorm/agents/stages.py` — `Stage.MEMORY_CONSOLIDATION`, new transitions
- `src/trendstorm/agents/state.py` — `MemoryConsolidationState`, `schema_version=3`
- `src/trendstorm/agents/orchestrator/edges.py` — `after_publish` + `after_memory_consolidation`
- `src/trendstorm/agents/orchestrator/nodes.py` — `memory_consolidation_node`
- `src/trendstorm/agents/orchestrator/graph.py` — new node + edges registered
- `src/trendstorm/orchestration/topics.py` — 5 new topics, `MEMORY_CONSOLIDATION` consumer group
- `src/trendstorm/orchestration/events.py` — `MemoryPendingEvent`, `MemoryCompletedEvent`
- `src/trendstorm/orchestration/workers/orchestrator_worker.py` — `_handle_publish_completed` (→ MEMORY_CONSOLIDATION), `_handle_memory_completed` (→ COMPLETED)
- `src/trendstorm/orchestration/workers/analyst_worker.py` — wired `MemoryRetriever`
- `src/trendstorm/services/analysis/analyst.py` — parallel memory retrieval in `produce_analysis`
- `src/trendstorm/infrastructure/mongo/schema.py` — `Collection.MEMORIES`
- `src/trendstorm/infrastructure/mongo/indexes.py` — 6 new memory indexes
- `src/trendstorm/infrastructure/mongo/repositories/__init__.py` — `MongoMemoryRepository` export
- `src/trendstorm/infrastructure/mongo/repositories/analysis_repository.py` — `iter_completed` (backfill)
- `src/trendstorm/api/main.py` — memories router wired
- `docker/docker-compose.yml` — 5 new Kafka topics in kafka-init
- `docker/docker-compose.app.yml` — `memory-consolidation-worker` service
- `sdk/python/src/trendstorm_sdk/_client.py` — `self.memories = MemoriesResource`
- `sdk/python/src/trendstorm_sdk/resources/__init__.py` — `MemoriesResource` export
- `.env.example` — `MEMORY__*` vars
- `Makefile` — `worker-memory`, `backfill-memories` targets
- `tests/unit/test_stages.py` — updated for MEMORY_CONSOLIDATION transitions
- `tests/unit/test_edges.py` — updated for new routing logic
- `tests/unit/test_hitl.py` — schema_version=3
- `tests/unit/test_orchestrator_analysis_handler.py` — publish handler now routes to MEMORY_CONSOLIDATION
- `tests/unit/test_orchestrator_graph_phase8.py` — full publish→memory→complete handoff sequence

## Metrics

| Metric | Type | Labels |
|---|---|---|
| `trendstorm_memory_writes_total` | Counter | tenant_id, kind, status |
| `trendstorm_memory_retrieval_hits` | Histogram | tenant_id, kind |
| `trendstorm_memory_consolidation_duration_seconds` | Histogram | tenant_id, status |
| `trendstorm_memories_active` | Gauge | tenant_id_hash, kind |
