# Mongo Connection Pool Saturation

**Alerts**: `MongoPoolSaturated` (>90% utilization for 5 minutes, page)

## Signal
`trendstorm_mongo_pool_utilization_ratio` exceeds 0.9 for `service`. This gauge is the fraction of `max_pool_size` connections currently checked out. At 100%, new operations queue and latency spikes.

## Impact
All services sharing the Mongo client experience increased latency. API p99 spikes first (visible in `trendstorm_api_request_duration_seconds`). Worker operations (repository inserts, lookups) stall mid-pipeline. Jobs slow down or time out.

## Diagnosis

1. **Grafana → Infrastructure → Mongo Pool**: which service? Is it a sudden spike or gradual climb?
2. **Loki** — look for slow operations holding connections:
   ```logql
   {service_name="trendstorm-api"} | json | duration_ms > 5000
   | line_format "{{.event}} {{.collection}} {{.duration_ms}}ms"
   ```
3. **Mongo shell** — find long-running operations:
   ```bash
   docker exec trendstorm-mongo mongosh -u root -p rootpass --authenticationDatabase admin \
     --eval 'db.adminCommand({currentOp: true, active: true, secs_running: {$gt: 5}})'
   ```
4. **Mongo explain plans** — check if a slow query is missing an index:
   ```bash
   docker exec trendstorm-mongo mongosh trendstorm -u root -p rootpass \
     --eval 'db.chunks.find({tenant_id: "xxx"}).explain("executionStats")'
   ```
   A `COLLSCAN` on a large collection is the typical culprit.

## Remediation

**Missing index causing full collection scan**:
1. Add the missing index to `infrastructure/mongo/indexes.INDEXES`.
2. Run `make seed-indexes` to apply idempotently.
3. This adds the index in the background (`background: true` is default in Motor); ongoing operations continue but the index build puts load on Mongo. In prod, schedule during low-traffic.

**Connection leak** (connections not returned to pool):
1. Every Motor operation is used in `async with` context managers — if code paths raise exceptions without closing cursors, connections leak. Audit recent changes to repository methods.
2. Restart the affected service to flush leaked connections; investigate the root cause before re-deploying.

**Surge in concurrent operations** (load spike):
1. Increase `MONGO__MAX_POOL_SIZE` (default 100). Restart affected services.
2. If the surge is expected (batch job, migration), consider rate-limiting the producer.

**Replica set primary failover**:
- After failover, the Motor client reconnects automatically but there's a brief window of refused connections. Pool utilization spikes during reconnect. If the spike is transient (< 30s), it's self-healing. If sustained, check replica set status: `docker exec trendstorm-mongo mongosh --eval 'rs.status()'`

## Prevention
- Track p99 Mongo operation latency: add `duration_ms` histogram to repository base class (Phase 11 instrumentation pass).
- Set a hard `maxTimeMS` on expensive aggregation queries to prevent runaway scans from holding connections indefinitely.
- Use `min_pool_size=10` (already configured) to pre-warm connections at startup, reducing the first-request latency spike.
