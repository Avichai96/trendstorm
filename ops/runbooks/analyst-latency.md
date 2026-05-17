# Analyst Latency

**Alerts**: `AnalystLatencyP99Page` (>300s, page), `AnalystLatencyP99Warning` (>180s)

## Signal
The p99 of `trendstorm_analyst_pass_duration_seconds` exceeds the SLO threshold. This measures wall-clock time from `handle()` entry to exit in the `AnalystWorker`, covering: query expansion LLM call + hybrid retrieval (BM25 + vector + RRF + rerank) + analyst LLM call + validator LLM call.

## Impact
Jobs stall in `ANALYZING` stage. SSE clients see `STAGE_STARTED` (analyzing) but never `REPORT_READY`. Jobs exceed their expected completion time. If `max_refinement_loops` is hit, a low-confidence report is still published, but it may be lower quality.

## Diagnosis

1. **Grafana → Analyst → Pass Duration**: confirm p99 spike and whether it's all tenants or one.
2. **Grafana → LLM → Call Duration p99**: isolate whether the LLM is slow or retrieval is slow.
3. **Jaeger → Service: trendstorm-analyst-worker → Operation: analyst.run_pass**: find the slowest span and expand child spans to identify the bottleneck (query expansion, retrieval, LLM completion, validation).
4. **Loki**:
   ```logql
   {service_name="trendstorm-analyst-worker"} | json
   | duration > 60s
   ```
5. Check the external LLM provider status page (Anthropic, Gemini, OpenAI) for incidents.

## Remediation

**LLM provider slow/degraded**:
1. Check `trendstorm_llm_calls_total{status="permanent_error"}` for the provider.
2. Switch `LLM__DEFAULT_CHAT_PROVIDER` to an alternative (e.g. `gemini` if `anthropic` is degraded). Restart `analyst-worker` container.
3. If Cohere reranker is slow, it's blocking — set `ANALYSIS__RERANKER=none` (add a flag) or temporarily remove the Cohere reranker config to fall back to RRF.

**Retrieval slow (ChromaDB or Mongo)**:
1. Check `trendstorm_vector_store_health` and `trendstorm_mongo_pool_utilization_ratio`.
2. See [chromadb-health.md](chromadb-health.md) or [mongo-pool.md](mongo-pool.md) as appropriate.

**Kafka consumer lag causing queue delay**:
- The analyst timer starts at `handle()` entry. If messages are queued in Kafka, the delay shows up in Kafka consumer lag, not in `analyst_pass_duration`. Check `trendstorm_kafka_consumer_lag_messages` for `analyst-worker`.

## Prevention
- Add `RetryingChatProvider` wrapper for transient LLM errors (pending polish item) to avoid long stalls on rate-limit retries.
- Budget each sub-operation with `asyncio.wait_for` timeouts in `Analyst.produce_analysis`.
- Test with production-scale corpora during load testing to calibrate SLO thresholds.
