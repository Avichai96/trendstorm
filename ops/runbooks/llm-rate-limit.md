# LLM Rate Limiting

**Alerts**: `LLMRateLimitSustained` (>10% permanent errors for a provider, page)

## Signal
`trendstorm_llm_calls_total{status="permanent_error"}` is more than 10% of total LLM calls for a provider over 3 minutes. This covers both `LLMRateLimitError` (429) and `LLMPermanentError` (auth failure, quota exhaustion).

## Impact
- **Anthropic rate-limited**: Analyst worker cannot generate analyses. Jobs stall in `ANALYZING`. After `max_refinement_loops` retries, a low-confidence report may publish or the job fails.
- **Gemini rate-limited**: Knowledge worker cannot embed documents. Jobs stall in `EMBEDDING`.
- **Auth failure**: All calls fail immediately. Provider is effectively down.

## Diagnosis

1. **Grafana → LLM → Calls/s by Provider**: check which provider is failing and at what rate.
2. **Grafana → LLM → Call Duration p99**: rate-limit errors are fast (429 response); a spike in error rate with low latency confirms rate limiting rather than provider slowness.
3. **Loki**:
   ```logql
   {service_name=~"trendstorm-analyst-worker|trendstorm-knowledge-worker"} | json
   |= "LLMRateLimitError" or "LLMPermanentError" or "permanent_error"
   | line_format "{{.error_message}} {{.model_id}}"
   ```
4. Check the provider's status page:
   - Anthropic: https://status.anthropic.com
   - Gemini: https://status.cloud.google.com
   - OpenAI: https://status.openai.com
5. Check current quota usage in the provider's console. `trendstorm_llm_input_tokens_total` and `trendstorm_llm_output_tokens_total` show cumulative token burn.

## Remediation

**Rate limit (429)**:
1. Check if the `RetryingEmbeddingProvider` is already backing off. Rate-limit errors go to Kafka retry topics (30s → 5m → 1h) before DLQ, so individual jobs will self-heal if the rate limit clears.
2. If quota is genuinely exhausted for the day: switch the affected worker to an alternative provider.
   - For embeddings: `KAFKA__DEFAULT_EMBEDDING_PROVIDER=ollama` (requires Ollama running); restart `knowledge-worker`.
   - For chat: `KAFKA__DEFAULT_CHAT_PROVIDER=gemini` (if Anthropic is rate-limited); restart `analyst-worker`.

**Auth failure (401/403)**:
1. Check that `LLM__ANTHROPIC_API_KEY` / `LLM__GEMINI__API_KEY` are set and not expired.
2. Rotate the key in the provider console, update `.env.local`, restart affected workers.

## Prevention
- Track daily token burn with `trendstorm_llm_input_tokens_total` rolling-24h counter in Grafana; alert before quota is exhausted (e.g. alert at 80% of daily quota).
- Implement `RetryingChatProvider` (pending polish item) to handle transient 429s at the call level rather than relying solely on Kafka-level retry.
- Spread load across providers: embeddings → Gemini, chat → Anthropic, with Ollama as local fallback.
