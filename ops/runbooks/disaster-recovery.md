# Disaster Recovery Runbook

**Scope**: Single-region deployment (us-east-1). Multi-region is ADR 001 (deferred).
**RPO target**: 1 hour (last MongoDB backup).
**RTO target**: 4 hours (infra reprovisioning + data restore + smoke test).

---

## Failure scenarios and procedures

### 1. Kafka topic data loss

**Symptoms**: Consumer lag spikes to 0 suddenly; workers idle; jobs not advancing.

**Cause**: Kafka log files corrupted or broker restarted with `log.retention.bytes` triggered unexpectedly.

**Recovery**:
1. Identify affected topics via Kafka UI or `kafka-topics.sh --describe`.
2. Jobs stuck at `INGESTING` or later can be replayed: set their status back to `PENDING` in Mongo (admin script: `scripts/replay_stuck_jobs.py`) and republish `JobRequestedEvent` via the outbox.
3. Jobs at `PENDING` (outbox not yet relayed): relay worker will republish automatically on next poll cycle once Kafka recovers.
4. DLQ entries: drain `dlq.v1` to ops Slack channel and assess per-message replay vs. manual retry.

**Prevention**: Kafka topic replication factor ≥ 3 (configured in `docker-compose.yml`; verify in production cluster settings).

---

### 2. MongoDB primary failure

**Symptoms**: Motor `ServerSelectionTimeoutError`; API returns 503; workers log `startup_failed` or `db_connection_error`.

**Cause**: MongoDB primary crashed or is unreachable.

**Recovery**:
1. Atlas / managed cluster: failover is automatic (30-60s). Monitor Atlas console.
2. Self-hosted replica set: check RS status via `rs.status()`. If primary is down, force election: `rs.stepDown()` on a secondary, or `rs.reconfig({...})` to remove the dead member.
3. If data loss occurred: restore from the most recent Atlas snapshot (daily, retained 7 days). Import to secondary, initiate resync.
4. After recovery: run `make seed-indexes` to verify indexes are intact (seeder is idempotent).

**Outbox resiliency**: The outbox relay polls MongoDB. If Mongo was down during Kafka outage, outbox entries are durable — they'll be relayed once both services recover.

---

### 3. Redis cluster failure

**Symptoms**: Rate limiting bypassed (RateLimitMiddleware falls back to pass-through); SSE streams stall; idempotency check failures.

**Cause**: Redis master unreachable or ElastiCache failover in progress.

**Recovery**:
1. ElastiCache: automatic failover to replica (< 60s). Application re-connects on next request.
2. On-prem Redis: promote replica manually: `SLAVEOF NO ONE`.
3. SSE impact: active SSE clients lose their Pub/Sub subscription. They will reconnect via the `Last-Event-ID` header and replay from Redis Streams if the stream data survived. If Streams data was lost, clients see no events — jobs may still complete successfully; users must refresh.
4. Idempotency keys are ephemeral — their loss means some Kafka messages may be processed twice. Workers are designed to be idempotent at the business logic level (dedup via `content_hash`, upsert semantics in ChromaDB).

---

### 4. MinIO / S3 unavailability

**Symptoms**: Scout worker fails with `BlobError`; knowledge worker cannot download text; publisher fails PDF/Markdown upload.

**Cause**: MinIO pod down or S3 service disruption.

**Recovery**:
1. Scout worker: `IngestionResult` records failed source outcomes. Re-trigger the job from PENDING (admin script) to re-ingest once blob store recovers.
2. Knowledge worker: raw text download fails → knowledge pipeline cannot chunk. The job will retry via Kafka retry topology (30s → 5m → 1h → DLQ). Manual intervention required only if retries exhaust.
3. Publisher: publishes `PublishCompletedEvent(success=False)`. The orchestrator marks the job FAILED. Admin can re-trigger publishing once blob recovers by setting job to `PUBLISHING` state and republishing `PublishPendingEvent` via outbox.

---

### 5. Complete cluster loss (nuclear scenario)

**Steps**:
1. Provision new Kubernetes cluster using Terraform/Pulumi (infrastructure code not yet in this repo — ADR 001 tracks this).
2. Apply ExternalSecrets: `kubectl apply -f k8s/secret-store.yaml`. Verify secrets sync.
3. Apply NetworkPolicies: `kubectl apply -f k8s/network-policies.yaml`.
4. Helm deploy: `helm upgrade --install trendstorm ./helm/trendstorm -f helm/trendstorm/values-production.yaml --set global.imageTag=<last-known-good-sha>`.
5. Restore MongoDB from snapshot to new Atlas cluster. Update `MONGO__URI` in SSM.
6. Run `make seed-indexes` against new cluster.
7. Run `scripts/smoke_test.py` against new API endpoint.
8. Update DNS (Route 53) to point to new ingress IP.
9. Monitor error rates in Grafana for 30 minutes.

---

## Backup schedule

| Resource         | Frequency | Retention | Location              |
|------------------|-----------|-----------|-----------------------|
| MongoDB          | Daily     | 7 days    | Atlas automated backup |
| MinIO/S3         | Daily     | 30 days   | S3 versioning enabled  |
| Kubernetes state | On-change | 90 days   | Velero → S3            |

## Contact

- Primary on-call: ops@trendstorm.ai
- Escalation: eng-leads@trendstorm.ai
- Post-mortem template: `ops/runbooks/postmortem-template.md`
