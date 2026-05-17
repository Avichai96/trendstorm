# Cost Overrun Runbook

## Signal

One or more of:
- Grafana alert `TrendStormLLMCostAnomaly`: per-tenant LLM spend > 2× daily average.
- Anthropic / OpenAI billing alert triggered.
- Tenant support ticket: "my quota is exhausted".

## Impact

- Affected tenant's job creates return HTTP 402 (quota exceeded).
- No financial impact to TrendStorm unless per-tenant cost > the tenant's plan limit.

## Diagnosis

1. **Identify the tenant(s) driving cost**:
   ```bash
   # Query cost_ledger for today's top spenders
   mongosh "$MONGO__URI" --eval '
     db.cost_ledger.aggregate([
       { $match: { created_at: { $gte: new Date(new Date().setHours(0,0,0,0)) } } },
       { $group: { _id: "$tenant_id", total: { $sum: "$cost_usd_micro" } } },
       { $sort: { total: -1 } },
       { $limit: 10 }
     ])
   '
   ```

2. **Identify the model and operation driving cost**:
   ```bash
   mongosh "$MONGO__URI" --eval '
     db.cost_ledger.aggregate([
       { $match: { tenant_id: "TENANT_ID_HERE", created_at: { $gte: ISODate("2026-05-01") } } },
       { $group: { _id: { provider: "$provider", model_id: "$model_id", stage: "$stage" }, total: { $sum: "$cost_usd_micro" } } },
       { $sort: { total: -1 } }
     ])
   '
   ```

3. **Check for runaway job loops** (analysis refinement loop exhausted without terminating):
   ```bash
   mongosh "$MONGO__URI" --eval 'db.jobs.find({ tenant_id: "TENANT_ID", status: "ANALYZING" }, { _id: 1, created_at: 1 }).sort({ created_at: -1 }).limit(10)'
   ```

## Remediation

### If the tenant is legitimately over quota
- The quota system (QuotaService) should have already rejected new jobs with HTTP 402.
- Verify the quota check is working: `GET /v1/quota` from a tenant context should show `allowed: false`.
- If not: check `PLAN_QUOTAS` in `domain/billing/models.py` vs. the tenant's plan in `tenants` collection.

### If there's a runaway analyst loop
- Find the job ID and set its status to FAILED in Mongo:
  ```bash
  mongosh "$MONGO__URI" --eval 'db.jobs.updateOne({ _id: "JOB_ID" }, { $set: { status: "FAILED", failure_code: "manual_termination", updated_at: new Date() } })'
  ```
- Check `max_refinement_loops` in `AnalysisSettings` (default 2). If loops exceed 2, something is wrong in the orchestrator's analysis-completed handler — see `orchestration/workers/orchestrator_worker.py:_handle_analysis_completed`.

### If an LLM provider is returning errors causing excessive retries
- Check `dlq.v1` for analyst failures.
- Temporarily set `LLM__DEFAULT_CHAT_PROVIDER=ollama` to stop using the expensive provider (local inference, no cost).
- File a ticket with the provider for billing credit if the errors were provider-side.

### If the price table is wrong
- Update `services/billing/prices.py` with correct per-token pricing.
- Retroactive cost correction: the ledger stores `cost_usd_micro` at write time. Historical entries cannot be corrected in place, but the billing report can be regenerated if needed.

## Prevention

- **Rate limit per tenant**: Redis token-bucket limits requests/min (see `AuthSettings.rate_limit_requests_per_minute`).
- **Job quota**: `QuotaService` blocks job creation when monthly job count or spend is exceeded.
- **RetryingChatProvider** (planned): limit transient error retries per job to avoid unbounded LLM retry costs on the hot path.
- **Cost alert**: set up provider-side billing alerts at 80% of the expected monthly budget.
