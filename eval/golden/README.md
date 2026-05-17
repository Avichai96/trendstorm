# TrendStorm Eval — Golden Dataset

This directory contains git-versioned evaluation fixtures for the analysis pipeline.
Each subdirectory is one `GoldenExample` (maps to the `GoldenExample` Pydantic model in
`src/trendstorm/domain/evaluation/models.py`).

## Directory layout

```
eval/golden/
  <example_name>/
    example.json     ← GoldenExample serialized to JSON (required)
    README.md        ← curation notes, why this example exists (optional but encouraged)
```

## Curation discipline

### What makes a good golden example

1. **Representative but small corpus.** 4–7 chunks per example. Enough for the
   analyst to form a grounded argument; small enough that eval runs stay fast.

2. **Realistic chunk text.** Chunks should read like actual scraped article
   excerpts — sentences, not bullet lists, not headings. Length: 100–300 words.
   Do NOT use lorem ipsum.

3. **Expected insights are semantic, not literal.** The claim in `ExpectedInsight`
   should capture the concept the analyst SHOULD surface, not copy-paste from a
   chunk. The coverage evaluator uses embedding similarity (≥ 0.70 cosine), so
   paraphrases pass. Over-specifying exact wording is fragile.

4. **Mark `required: true` conservatively.** Only mark an insight required if
   ANY competent analysis of the provided corpus MUST surface it. Optional
   insights (required: false) improve the score but do not gate pass/fail.

5. **`summary_keywords`** should be 3–6 terms that a relevant analysis summary
   would naturally contain. These are checked by the relevance evaluator for
   keyword presence as a quick sanity signal — not as the primary relevance score.

6. **`min_citations`** should be at least 1. Most analyses should cite 2–3 chunks.
   Set to len(chunks) only if you expect every chunk to be referenced.

### What to avoid

- Trick examples designed to fool the LLM. These belong in adversarial suites
  (Phase 12), not the regression golden set.
- Examples where the correct answer is highly opinion-dependent. Golden examples
  test grounding and coverage, not editorial quality.
- Very long chunk text (> 400 words). The analyst truncates excerpts to 500 chars;
  very long chunks test chunker behavior, not analyst behavior.
- Duplicate chunk IDs across examples. Each `chunk_id` must be unique within an
  example (cross-example uniqueness is not enforced but avoids confusion).

### Adding new examples

1. Create `eval/golden/<slug>/example.json` by hand or by running:
   ```
   uv run python scripts/seed_golden.py --category "..." --name <slug>
   ```
2. Validate the schema:
   ```
   uv run python -c "
   import json; from trendstorm.domain.evaluation.models import GoldenExample
   GoldenExample.model_validate(json.load(open('eval/golden/<slug>/example.json')))
   print('OK')
   "
   ```
3. Add a `README.md` in the example directory explaining why this example exists
   and what failure modes it is designed to catch.
4. Open a PR. Golden examples are reviewed like code — they are the regression
   baseline for the analyst prompt. A bad golden example is worse than no example.

### Updating existing examples

When the analyst prompt changes and the expected behavior changes legitimately:
1. Run the full eval suite BEFORE and AFTER (`make eval-full`).
2. If the change causes a regression on existing goldens, decide: is the golden
   wrong, or is the prompt wrong?
3. If updating the golden, note the reason in the example's `README.md` and in
   the PR description.

### Versioning

Golden examples are version-controlled in git alongside the code. There is no
"golden example database" — git blame is the audit trail. Breaking changes to the
`GoldenExample` schema require migrating all existing JSON files.

## Running eval

```bash
# Fast suite (unit-level, mock LLM judges)
make eval-fast

# Full suite (real LLM judges, requires ANTHROPIC_API_KEY / GEMINI_API_KEY / OPENAI_API_KEY)
make eval-full

# CI gate (reads artifacts/eval-*.json, exits non-zero on threshold violation)
make eval-check
```

## Threshold configuration

Thresholds live in `EvalSettings.thresholds` (env vars: `EVAL__THRESHOLDS__FAITHFULNESS`,
`EVAL__THRESHOLDS__CITATION_ACCURACY`, etc.). Current defaults:

| Dimension         | Default threshold |
|-------------------|-------------------|
| faithfulness      | 0.85              |
| citation_accuracy | 0.95              |
| relevance         | 0.80              |
| coverage          | 0.70              |

Raising a threshold is a conscious quality commitment. Lower a threshold only with
explicit sign-off — it is a regression in the product contract.
