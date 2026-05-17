# Runbook: Production Eval Sampling Flag

**Alert**: `TrendstormProductionEvalFlagged`
**Severity**: Warning
**Oncall rotation**: AI quality team

---

## Signal

```
rate(trendstorm_eval_flagged_analyses[1h]) > 0
```

Or: `evaluations` collection in Mongo has documents with `flagged: true`.

The production eval worker (`production-eval-worker`) evaluates 1% of sampled
production analyses. An analysis is flagged when `aggregate_score < 0.65` (below
the operational threshold for human review).

---

## Impact

Flagged analyses indicate the analyst produced output that scored below the human-review
threshold on at least one dimension. This does not necessarily mean the analysis was
served incorrectly — it means it warrants human inspection.

At 1% sampling, 1 flagged analysis per hour implies ~100 total analyses per hour with
similar quality issues. At 10+ flagged per hour, consider whether a full regression
investigation is warranted (see [eval-regression.md](eval-regression.md)).

---

## Diagnosis

### 1. Query flagged analyses

```javascript
// Mongo query (via Mongo Express or mongosh)
db.evaluations.find(
  { flagged: true },
  { analysis_id: 1, tenant_id: 1, aggregate_score: 1, dimension_scores: 1, created_at: 1 }
).sort({ created_at: -1 }).limit(20)
```

### 2. Pull the flagged analysis

```javascript
db.analyses.findOne({ _id: "<analysis_id from above>" })
```

Examine:
- `insights`: are they grounded (do the chunk_ids exist and match the claim)?
- `citations`: do excerpts match chunk text?
- `validator_score` and `validator_notes`: what did the validator flag?

### 3. Check dimension breakdown

The `evaluations` document has `dimension_scores` — check which dimension is failing:
- `faithfulness < 0.85`: claims not supported by chunks
- `citation_accuracy < 0.95`: excerpt doesn't match chunk text
- `relevance < 0.80`: analysis off-topic

### 4. Check when the pattern started

```bash
# Check the eval worker logs for the time the flagging started
docker logs trendstorm-production-eval --since 2h 2>&1 | grep "aggregate_score\|flagged"
```

Compare with:
- Recent deploys: `git log --oneline --since '2 hours ago'`
- Recent Kafka topic lag: check Kafka UI for `analysis.completed.v1`

---

## Remediation

### High flagging rate (>5% of sampled analyses flagged in a rolling hour)

This suggests a systemic issue, not an isolated quality dip:

1. Check if a new analyst prompt was deployed recently. If so, consider reverting.
2. Check if the embedding provider (Gemini/Ollama) has degraded — low-quality
   embeddings cause poor retrieval which causes low-quality analyses.
3. Run `make eval-fast` locally to confirm the regression is reproducible.
4. If confirmed: follow [eval-regression.md](eval-regression.md) remediation steps.

### Isolated flagging (1-2 analyses per hour)

This is within normal variance. Log the analysis IDs for the weekly quality review.
No immediate action required unless the flagging is concentrated on one tenant or
category (which may indicate a domain-specific retrieval failure).

### Sampling rate adjustment

The 1% sample rate is controlled by the `EVAL__PRODUCTION_SAMPLE_RATE` env var
(default: 0.01). The sampling decision is `hash(job_id) % 100 == 0` (deterministic
per job, consistent across retries).

To increase sampling temporarily during investigation:
```
EVAL__PRODUCTION_SAMPLE_RATE=0.05  # 5%
```
Restart the `production-eval-worker` service. This increases evaluation load by 5×
— ensure the eval worker has sufficient capacity (it makes LLM calls).

To disable production eval sampling entirely (emergency only):
```
EVAL__PRODUCTION_SAMPLE_RATE=0.0
```

---

## Prevention

- Keep the eval worker's `min_quorum=2` so a single failing judge doesn't produce
  spurious flags. If a judge provider is down, the panel degrades gracefully to the
  remaining judge(s).
- The `EVAL__MIN_QUORUM` env var controls this at runtime.
- Monitor `trendstorm_llm_errors_total{operation="eval_panel"}` for judge failures
  — persistent failures indicate a provider API issue, not an analyst quality issue.
- Monthly: review the `evaluations` collection's flagged analyses and feed confirmed
  low-quality examples into the golden dataset to improve regression coverage.

---

## Escalation

If flagging rate exceeds 10% for more than 30 minutes:
1. Page AI quality team lead.
2. Dump flagged analysis IDs and their dimension scores.
3. Cross-reference with Kafka consumer lag on `analysis.completed.v1` — a lag spike
   may indicate a batch of low-quality analyses from a degraded model, not a systemic
   change.
