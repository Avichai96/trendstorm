# DLQ Spike

**Alerts**: `DLQSpikeHigh` (page), `DLQSpikeWarning`

## Signal
Messages are accumulating in `dlq.v1` faster than they're being drained. Either a worker is repeatedly failing and routing to DLQ, or a parsing error is poisoning a batch of events.

## Impact
Affected jobs stall at the stage that feeds the DLQ. If it's the scout DLQ, jobs stay `INGESTING` indefinitely. If it's the analyst DLQ, jobs stay `ANALYZING`. SSE clients see no terminal event and time out on their end.

## Diagnosis

1. **Grafana → TrendStorm Overview → Infrastructure → Kafka Consumer Lag**: identify which topic has the spike.
2. **Loki query** — filter by worker producing DLQ entries:
   ```logql
   {service_name=~"trendstorm-.*"} |= "dlq_send_failed" or "parse_failed_to_dlq" or "handler_domain_error"
   | json | line_format "{{.worker}} {{.error_code}} {{.error_message}}"
   ```
3. **Jaeger** — search for traces with `status=ERROR`; look for the span that raised first.
4. **Check the DLQ topic directly**:
   ```bash
   docker exec trendstorm-kafka kafka-console-consumer.sh \
     --bootstrap-server localhost:9092 \
     --topic dlq.v1 --from-beginning --max-messages 10
   ```
   The message headers `x-dlq-reason` and `x-dlq-detail` (set by `BaseConsumer._send_to_dlq`) identify the cause.

## Remediation

1. **Parse error** (`x-dlq-reason: parse_error`): An event schema changed without a migration. Check which producer published the malformed event (look at `event_type` in the message). Roll back the producer or update the consumer's `AnyEvent` union.
2. **Domain error** (e.g. `x-dlq-reason: not_found`): A referenced entity (Category, Job) no longer exists. The job is orphaned — mark it `FAILED` manually via the Mongo shell, then notify the tenant.
3. **Handler exception** (`x-dlq-reason: handler_exception`): A worker bug. Check the stack trace in Loki, deploy a fix, then replay the DLQ messages via a one-off consumer once the fix is deployed.
4. **Drain the DLQ** after resolving the root cause:
   ```bash
   # Replay DLQ to the original topic (adjust --topic as needed)
   docker exec trendstorm-kafka kafka-console-producer.sh \
     --bootstrap-server localhost:9092 --topic <original-topic> < dlq-dump.json
   ```

## Prevention
- Add schema version to `EventEnvelope` and reject unknown versions at parse time with a clear error (rather than a `ValidationError` that only shows in DLQ headers).
- Write integration tests for every new event type that exercise the full parse → handle path.
- Set up a DLQ consumer dashboard panel that graphs `x-dlq-reason` label distribution.
