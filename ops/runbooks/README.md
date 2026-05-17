# TrendStorm Runbooks

Each runbook follows the same five-section template:

1. **Signal** — which alert fired and what it means
2. **Impact** — what users or tenants experience
3. **Diagnosis** — where to look first (Grafana dashboard, Jaeger, Loki queries)
4. **Remediation** — ordered steps to stop the bleeding
5. **Prevention** — what would stop this from recurring

Runbooks are configuration. Keep them accurate; stale runbooks are worse than none.

## Index

| Alert | Runbook |
|-------|---------|
| DLQSpikeHigh / DLQSpikeWarning | [dlq-spike.md](dlq-spike.md) |
| AnalystLatencyP99Page | [analyst-latency.md](analyst-latency.md) |
| PublisherLatencyP99Page | [publisher-latency.md](publisher-latency.md) |
| APILatencyP99Page / APIErrorRateHigh | [api-latency.md](api-latency.md) |
| LLMRateLimitSustained | [llm-rate-limit.md](llm-rate-limit.md) |
| KafkaConsumerLagHigh | [kafka-lag.md](kafka-lag.md) |
| ChromaDBUnhealthy | [chromadb-health.md](chromadb-health.md) |
| MongoPoolSaturated | [mongo-pool.md](mongo-pool.md) |
