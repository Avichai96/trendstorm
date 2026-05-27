# HITL Review Aging

**Alerts**: `PendingReviewsAgingHigh` (>80% of SLA, page), `PendingReviewsAgingWarning` (>24h), `ReviewTimeoutSweepSpike` (sweeper expiring reviews at high rate)

## Signal

`PendingReviewsAgingHigh` fires when the oldest pending `ReviewRequest` for any tenant has been waiting more than 38.4 hours (80% of the default 48h SLA). This means an analysis is paused in `AWAITING_REVIEW` and will auto-reject unless a reviewer acts.

`ReviewTimeoutSweepSpike` fires when `trendstorm_review_timeout_total` is incrementing at more than 2/min over 15 minutes — either a backlog of expired reviews is draining, or reviewers are consistently missing the SLA.

## Impact

- Jobs stuck in `AWAITING_REVIEW` block SSE clients on `review_required` — they see no `REPORT_READY`.
- Auto-reject on timeout sends `JOB_REJECTED` SSE event and marks the job `REJECTED`. The tenant must re-submit the job.
- A spike of timeouts indicates systemic reviewer availability failure — many tenants are blocked.

## Diagnosis

### 1. Identify the stuck review

```bash
# List oldest pending reviews (requires reviewer role on your API key)
curl -H "X-Tenant-ID: <tenant>" -H "Authorization: Bearer <key>" \
  "http://localhost:8080/v1/reviews?status=pending&limit=20"
```

In Grafana → HITL panel: check `trendstorm_reviews_pending` gauge by tenant bucket and the review age metrics.

### 2. Check the sweeper worker health

```bash
# sweeper logs
docker logs trendstorm-review-timeout-worker --tail=100 | grep -E "error|sweep|expired"

# sweeper metrics (port 9090 on the container)
curl http://localhost:9090/metrics | grep review_timeout
```

If the sweeper is down: pending reviews past SLA will NOT auto-reject until it restarts. This is a degraded state — reviewers must act manually or the sweeper must be restarted.

### 3. Check orchestrator connectivity

The sweeper publishes `review.resolved.v1` to Kafka. If the orchestrator is not consuming it:

```bash
docker logs trendstorm-orchestrator-worker --tail=50 | grep review_resolved
# Check Kafka consumer lag for orchestrator group
```

### 4. Distinguish backlog drain from ongoing miss

Look at `rate(trendstorm_review_timeout_total[5m])` over time:
- A one-time spike after sweeper restart = backlog drain (normal after downtime)
- Sustained rate = reviewers are consistently not meeting SLA

### 5. Find the job for a given review

```bash
# Get review detail including job_id
curl -H "X-Tenant-ID: <tenant>" -H "Authorization: Bearer <key>" \
  "http://localhost:8080/v1/reviews/<review_id>"
```

## Remediation

### Reviewer unavailable — extend SLA

If the tenant needs more time and their settings allow:

```python
# Adjust hitl_timeout_hours on TenantSettings via MongoDB directly (ops emergency)
db.tenant_settings.findOneAndUpdate(
  { tenant_id: "<tenant_id>" },
  { $set: { hitl_timeout_hours: 96 } }
)
```

Note: this does NOT retroactively extend `timeout_at` on existing `ReviewRequest` documents. Only new reviews inherit the updated timeout. For an in-flight review, update `timeout_at` directly:

```python
db.reviews.findOneAndUpdate(
  { _id: "<review_id>", status: "pending" },
  { $set: { timeout_at: ISODate("2026-...") } }
)
```

### Force-resolve a stuck review (operator decision)

If no reviewer is available and auto-reject is not appropriate:

```bash
# Resolve via API with approve decision (requires reviewer role)
curl -X POST \
  -H "X-Tenant-ID: <tenant>" \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  "http://localhost:8080/v1/reviews/<review_id>/resolve" \
  -d '{"decision": "approve", "comment": "Operator-approved: reviewer unavailable within SLA"}'
```

### Sweeper is down — restart

```bash
docker compose -f docker/docker-compose.app.yml restart review-timeout-worker
```

If the sweeper has been down for an extended period, it will process the backlog on restart. `ReviewTimeoutSweepSpike` may fire — this is expected and not an incident.

### Sustained miss — reviewer capacity

If `PendingReviewsAgingWarning` fires consistently, reviewers are not meeting the 24h informal target:

1. Add more reviewer-role API keys for the tenant.
2. Consider reducing `hitl_timeout_hours` to force faster escalation.
3. Consider switching the tenant's `hitl_mode` from `always` to `flagged_only` so only high-risk analyses require review.

## Prevention

- Set up reviewer on-call rotation if `hitl_mode = always` is used for high-throughput tenants.
- The `PendingReviewsAgingHigh` alert fires at 80% of SLA — 9.6 hours before auto-reject. This is the action window.
- If review volume is high, consider deploying a review UI that sends Slack/email notifications when `review_required` SSE events are emitted.
