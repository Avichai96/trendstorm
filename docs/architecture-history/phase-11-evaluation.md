# Phase 11 — Evaluation Pipeline

**Status**: ✅ Complete

## Summary

**Token accounting** (prerequisite): `StructuredChatProvider.complete_with_tools` return type extended from 2-tuple `(name, args)` to 3-tuple `(name, args, TokenUsage)`. `TokenUsage` (frozen Pydantic model: `input_tokens`, `output_tokens`, `cached_tokens`) added to `domain/llm/models.py`. All three chat providers (`AnthropicChatProvider`, `GeminiChatProvider`, `OpenAIChatProvider`) return real token counts; Anthropic also captures `cache_read_input_tokens`. `AnalystWorker` now calls `record_llm_cost()` with real counts and stamps `input_tokens`/`output_tokens` on the persisted Analysis. Validator unpacks and discards the usage tuple.

**Kafka eval topic**: `trendstorm.eval.sample.v1` (3 partitions, 30d retention) added to `docker-compose.yml` and `Topic`/`ConsumerGroup` enums. `EvalSampleEvent` added to the `AnyEvent` discriminated union.

**Config**: `EvalSettings` and `EvalThresholds` in `shared/config` — thresholds per dimension (faithfulness=0.85, citation_accuracy=0.95, relevance=0.80, coverage=0.70), `panel_judges`, `min_quorum`, `production_sample_rate` (default 0.01). `LangSmithSettings` added with `api_key`, `project`.

**Domain models** (`domain/evaluation/`): `EvalDimension` StrEnum (FAITHFULNESS, CITATION_ACCURACY, RELEVANCE, COVERAGE), `DimensionScore`, `EvaluationResult`, `GoldenChunk`, `ExpectedInsight`, `ExpectedAnalysis`, `GoldenExample`, `DimensionSummary`, `EvalRunReport`. `Evaluator` Protocol. `LLMJudge` Protocol, `JudgeVote`, `PanelAggregation` StrEnum, `PanelResult`, `PanelInsufficientVotesError`.

**Eval panel** (`services/evaluation/panel.py`): `LLMPanel` — concurrent `asyncio.gather(return_exceptions=True)` over N judges, quorum enforcement via `min_quorum`, module-level `_aggregate_votes()` for testable aggregation (MEAN/MEDIAN/MIN/MAJORITY). One failed judge doesn't abort the panel.

**Evaluators** (`services/evaluation/evaluators/`): `CitationLookupEvaluator` (deterministic — embedding cosine similarity between excerpt and chunk text, falls back to `GoldenExample.chunks` when no Mongo repo provided), `GoldenCoverageEvaluator` (deterministic — embedding similarity recall against `ExpectedInsight` list; raises ValueError on production samples without golden), `LLMPanelFaithfulnessEvaluator` (per-insight panel scoring, mean of scores), `LLMPanelRelevanceEvaluator` (single panel call on analysis summary vs. category brief). Prompts in `services/evaluation/prompts/faithfulness_judge.md` and `relevance_judge.md`.

**LangSmith client** (`infrastructure/langsmith/client.py`): lifecycle wrapper around `langsmith.Client` SDK. Graceful no-op when API key absent. `push_eval_results(report)` is best-effort (failures logged as warnings, never raised). `list_examples`, `create_dataset` for dataset management.

**EvalRunner** (`services/evaluation/runner.py`): orchestrates `dataset × evaluators`. `run_eval(dataset, target, suite, project)` runs all examples → aggregates per-dimension means → detects threshold violations → persists to `artifacts/eval-{timestamp}-{run_id[:8]}.json` → pushes to LangSmith best-effort. ValueError from evaluators (GoldenCoverageEvaluator on prod samples) silently skipped; other exceptions record 0.0 and continue.

**Golden dataset** (`eval/golden/`): 3 initial examples (ai_safety_rlhf, llm_interpretability, ai_governance) each with 5 chunks and an `ExpectedAnalysis` with 4-5 insights (3 required, 1-2 optional). Each example has a `README.md` explaining its purpose and failure modes. `eval/golden/README.md` documents curation discipline, threshold table, and curation process.

**CLI** (`scripts/run_eval.py`): `--suite fast` (deterministic evaluators only, no LLM keys) or `--suite full` (all evaluators). Exit code 0=pass, 1=violation, 2=error. Prints per-dimension summary table.

**Analyst worker 1% sampling** (`orchestration/workers/analyst_worker.py`): after a `passed=True` analysis, checks `hash(job_id) % 100 == 0` — deterministic per job. On match, publishes `EvalSampleEvent` to `trendstorm.eval.sample.v1`. Sampling is best-effort (exception never crashes business logic).

**Production eval worker** (`orchestration/workers/production_eval_worker.py`): `ProductionEvalWorker(BaseConsumer)`. Consumes `eval.sample.v1`. Idempotency key `f"prod_eval:{job_id}:{analysis_id}"`. Loads Analysis from Mongo, runs CitationLookupEvaluator + optional LLM panel (when API keys present). `ProductionEvalPipeline` (`agents/production_eval/pipeline.py`) handles the per-analysis logic. Evaluation results persisted to `evaluations` collection in Mongo.

**MongoDB**: `Collection.EVALUATIONS` added to schema; 4 indexes (tenant+created, tenant+analysis_id, flagged partial, TTL 1y). `docker/production_eval.Dockerfile` (multi-stage, `llm+rag+eval` groups). `docker-compose.app.yml` adds `production-eval-worker` service.

**Makefile**: `eval-fast`, `eval-full`, `eval-check` targets.

**Runbooks**: `ops/runbooks/eval-regression.md` and `ops/runbooks/production-eval-flag.md`.

**Tests**: 894 unit tests (+39 from Phase 11). `tests/unit/test_panel_aggregation.py` (24 tests — all aggregation strategies, quorum enforcement, edge cases). `tests/unit/test_citation_evaluator.py` (15 tests — cosine similarity, golden chunk fallback, Mongo repo duck type, error handling). `tests/integration/test_eval_runner.py` (11 tests — single/multi-example, threshold violations, evaluator error handling, artifact persistence). `tests/integration/test_production_eval_worker.py` (12 tests — idempotency key, dispatch, sampling determinism, pipeline skipping logic).
