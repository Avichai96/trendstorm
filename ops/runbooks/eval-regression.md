# Runbook: Eval Regression

**Alert**: `TrendstormEvalRegression`
**Severity**: Warning (degraded quality signal; not customer-impacting in real-time)
**Oncall rotation**: AI quality team (primary), platform team (escalation)

---

## Signal

```
trendstorm_eval_threshold_violations > 0
```

Or: `make eval-check` exits non-zero in CI.

Artifacts are written to `artifacts/eval-{timestamp}.json`. The threshold violation
list in the artifact is the source of truth.

---

## Impact

A threshold violation means one or more eval dimensions has fallen below the configured
floor. Depending on which dimension:

| Dimension         | Customer impact if violated                                        |
|-------------------|--------------------------------------------------------------------|
| faithfulness      | Analyst generating ungrounded claims — hallucination risk HIGH     |
| citation_accuracy | Cited excerpts don't match the chunks — citation integrity broken  |
| relevance         | Analyst drifting off-topic — report quality degraded               |
| coverage          | Key insights consistently missed — report completeness degraded    |

A faithfulness or citation_accuracy regression is the most critical and warrants
immediate investigation.

---

## Diagnosis

### 1. Identify which dimension violated

```bash
# Read the latest eval artifact
cat artifacts/eval-$(ls artifacts/ | sort | tail -1) | python3 -m json.tool | grep -A 5 threshold_violations
```

Or check the CI job's `make eval-check` output — it prints each violated dimension.

### 2. Check which examples failed

```bash
# Re-run the fast suite with verbose output
make eval-fast 2>&1 | grep -E "FAIL|score|coverage"
```

If the full suite was run, the LangSmith project `trendstorm-eval` has per-example
breakdowns with judge rationales.

### 3. Identify what changed

```bash
# What changed in the analyst prompt or evaluator code?
git log --oneline services/analysis/prompts/ services/evaluation/ src/trendstorm/agents/ | head -20

# Did a model upgrade change the eval set?
git log --oneline pyproject.toml | head -10
```

### 4. Check if it's a real regression or a golden example drift

Compare the failed golden example against the current analyst output manually:
```bash
uv run python -c "
import json
data = json.load(open('artifacts/$(ls artifacts/ | sort | tail -1)'))
for s in data['dimension_summaries']:
    print(s['dimension'], s['mean_score'], s['pass_rate'])
"
```

If all dimensions regressed simultaneously → model change or prompt change.
If only one dimension regressed → dimension-specific issue.

---

## Remediation

### Option A: Prompt regression (faithfulness or relevance dropped)

1. Check recent changes to `services/analysis/prompts/analyst_system.md`.
2. Run the full eval suite on the previous commit:
   ```bash
   git stash && make eval-full && git stash pop && make eval-full
   ```
3. If the previous commit passed and HEAD fails → revert the prompt change.
4. If both fail → the regression predates the change (check golden examples).

### Option B: Model change degradation

1. If a new LLM provider or model version was deployed, compare scores before/after.
2. Consider pinning the model version in `EvalSettings.panel_judges` for the eval
   suite to use a stable reference model.
3. Do NOT raise the eval threshold to hide a regression.

### Option C: Golden example drift (expected behavior changed)

If the analyst behavior changed intentionally and the golden examples need updating:
1. Run the full suite and collect new outputs.
2. Review each failed example and decide: is the new output better, the same, or worse?
3. If better: update the golden `expected_analysis` and note the reason in the
   example's `README.md`. Requires team review.
4. If worse: do not update the golden — fix the regression first.

### Option D: Evaluator bug

If the evaluator itself is producing wrong scores (e.g. embedding provider returns
all-zero vectors due to a connection issue):
1. Check the `trendstorm.production-eval` logs for embedding errors.
2. Run `make eval-fast` locally with a known-good analysis to verify evaluator output.
3. Fix the evaluator bug before re-running.

---

## Prevention

- Run `make eval-fast` in CI on every PR that touches: `services/analysis/prompts/`,
  `services/evaluation/`, `agents/orchestrator/`, or `pyproject.toml`.
- Add `make eval-check` as a required CI gate (artifacts committed or checked in CI).
- When raising a threshold, document the reason in `CLAUDE.md` section 4 and in the
  PR description.
- Never lower a threshold without team sign-off — it is a regression in the product
  quality contract.

---

## Escalation

If the regression is in `faithfulness` or `citation_accuracy` AND affects production
traffic (the production eval worker is firing alerts from 1% sampling):
1. Page the AI quality team lead.
2. Consider disabling new job creation temporarily while investigating.
3. Do not deploy new analyst prompts until the root cause is confirmed.
