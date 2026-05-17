# Phase 4 — LangGraph Orchestration Backbone

**Status**: ✅ Complete

## Summary

`agents/stages.py` — `Stage` StrEnum with `is_valid_transition()` state machine. Self-loops allowed for retry semantics. `ANALYZING → RETRIEVING` is the only legal backward edge (refinement loop).
`agents/state.py` — `JobState` (schema_version=1) with identity fields, per-stage substates (IngestionState/KnowledgeState/RetrievalState/AnalysisState/PublishingState), `attempts: dict[Stage,int]`, `retry_budgets`, `refinement_loops`, `ObservabilityContext`, reference types (SourceRef/DocumentRef/ChunkRef/StageError). `DEFAULT_RETRY_BUDGETS = {INGESTING:5, EMBEDDING:3, RETRIEVING:3, ANALYZING:2, PUBLISHING:3}`. `MAX_REFINEMENT_LOOPS=2`. Helpers: `remaining_budget`, `has_budget`, `can_refine`, `JobState.initial()`.
`agents/orchestrator/{nodes,edges,graph,checkpointer}.py` — 8 nodes (init/ingest/embed/retrieve/analyze/refine/publish/fail), conditional edges with retry+refinement routing. **Nodes are STUBS**; real impls Phase 6+. `MongoCheckpointer` wraps **sync** `MongoDBSaver` (NOT `AsyncMongoDBSaver` — that import doesn't exist) and owns a dedicated sync pymongo connection. Constructor: `MongoCheckpointer(settings: MongoSettings)`.
`orchestration/{topics,events,workers}/`, `infrastructure/kafka/consumer.py` — `Topic` and `ConsumerGroup` StrEnums, Pydantic `EventEnvelope` with discriminated union `AnyEvent`. `BaseConsumer` does manual offset commits, parses with TypeAdapter (→ DLQ on parse fail), extracts traceparent for span continuation, `_dispatch_with_idempotency` (acquire→handle→complete). Signal handling for graceful SIGTERM. `OrchestratorWorker` overrides `_idempotency_key` to return `None` — LangGraph checkpointer is the idempotency layer for this worker; per-message dedup would BLOCK legitimate resumes.
`services/job_service.py` — persist Job → inject traceparent → publish `JobRequestedEvent`. Outbox pattern deferred to Phase 12.
`api/routers/jobs.py` — POST `/v1/jobs` (202 + stream_url), GET `/v1/jobs/{id}`, GET `/v1/jobs` (cursor pagination).
