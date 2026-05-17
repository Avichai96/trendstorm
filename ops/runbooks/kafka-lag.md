# Kafka Consumer Lag

**Alerts**: `KafkaConsumerLagHigh` (>1000 messages, page), `KafkaConsumerLagWarning` (>200)

## Signal
`trendstorm_kafka_consumer_lag_messages` for a specific `service/topic` pair exceeds the threshold. The metric is updated by each worker via `METRICS.kafka_consumer_lag` gauge (set per polling interval).

## Impact
Messages queue up in Kafka. Depending on the worker and topic:
- **orchestrator-worker / jobs.requested.v1**: new jobs are not started; users see their job stuck in `REQUESTED` status.
- **scout-worker / ingest.pending.v1**: jobs stuck in `INGESTING`.
- **knowledge-worker / knowledge.pending.v1**: jobs stuck in `EMBEDDING`.
- **analyst-worker / analysis.pending.v1**: jobs stuck in `ANALYZING`.
- **publisher-worker / publish.pending.v1**: jobs stuck in `PUBLISHING`.
- **sse-coordinator-worker / stream.partial.v1**: SSE clients see gaps in the event stream.

## Diagnosis

1. **Grafana → Infrastructure → Kafka Consumer Lag**: which service/topic and trending up or stable?
2. **Worker logs** — look for crash-loop or slow processing:
   ```bash
   docker logs trendstorm-<worker> --tail 100
   ```
3. **Check worker is running**: `docker ps | grep trendstorm-<worker>`
4. **Check Kafka directly** (via Kafka UI at http://localhost:8080 if `make up-dev` is running):
   - Consumer group lag per partition.
   - Partition count vs. consumer count (should be 1:1 in single-worker mode).
5. **Check if the lag is stable or growing**: a stable lag with slow processing is different from a crash loop.

## Remediation

**Worker crashed** (container not running or restarting):
1. `docker restart trendstorm-<worker>`
2. Check logs for the crash reason: OOM, dependency connection failure, unhandled exception.

**Worker running but processing too slowly** (LLM calls, large documents):
1. For temporary overload: Kafka will buffer; wait for the batch to drain.
2. For sustained overload: scale the worker (add partitions, add consumer instances). Note: `auto.create.topics=false` — adding partitions requires explicit Kafka partition reassignment.

**Kafka itself unhealthy**:
1. `docker logs trendstorm-kafka --tail 50`
2. Check disk space (KRaft mode stores logs on disk; full disk = Kafka stops).

**Retry topic lag growing** (retry.*.30s, retry.*.5m, retry.*.1h):
- Retry lag means messages are being retried. Look at the DLQ to see if retries are eventually DLQ-ing. See [dlq-spike.md](dlq-spike.md).

## Prevention
- Deploy multiple consumer instances per worker for high-throughput topics (analyst, knowledge). Requires partition count ≥ consumer count.
- Monitor retry topic lag separately from main topic lag — a full retry queue is an early warning of a worker problem before it becomes critical lag on the main topic.
