# Golden Example: AI Governance & Policy

## Purpose

Provides an "easy" baseline regression test where the three required insights map
cleanly to separate chunks with minimal conceptual overlap. If the analyst fails
this example, the problem is likely a basic retrieval or citation failure, not a
nuanced reasoning issue. Useful as a sanity check after changes to the retrieval
pipeline or prompt structure.

## What failure looks like

- **Coverage failure**: analyst surfaces risk tiers and compute thresholds but
  omits the incident reporting finding (required). This would indicate the analyst
  is truncating its analysis at two findings.
- **Citation failure**: analyst attributes the 10^25 FLOP threshold claim to
  `gov_c01` (risk tiers chunk) instead of `gov_c02` (frontier models chunk).
- **Faithfulness failure**: analyst claims the US has mandatory incident reporting
  requirements — the corpus explicitly states the US framework is voluntary.

## Corpus notes

Five chunks: EU AI Act risk tiers, EU AI Act frontier model provisions, comparative
incident reporting across jurisdictions, international coordination landscape, and
compute governance. The international coordination chunk (gov_c04) and the compute
governance chunk (gov_c05) are present as optional context. gov_c05 is marked
`required: false` — it's relevant but secondary to the three main regulatory findings.

## Difficulty rationale

Marked "easy" because: (1) the three required insights each have a dedicated source
chunk with no conceptual ambiguity, (2) the category keywords directly match terms
in the chunks, and (3) the "wrong citation" failure mode requires a more serious
confusion. Use this example to verify that basic recall and citation accuracy are
working before investigating failures in harder examples.
