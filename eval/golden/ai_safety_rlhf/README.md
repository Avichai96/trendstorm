# Golden Example: AI Safety — RLHF

## Purpose

Tests that the analyst correctly surfaces the three major RLHF alignment findings
(reward hacking, scalable oversight, constitutional AI) from a corpus of five chunks.
Designed to catch regressions where the analyst generates plausible-sounding but
ungrounded insights, or misses a required finding because it focused on the last
chunk read.

## What failure looks like

- **Faithfulness failure**: analyst claims RLHF is "solved" or overstates results
  beyond what the chunks support.
- **Coverage failure**: analyst surfaces reward hacking and constitutional AI but
  omits scalable oversight (required).
- **Citation failure**: analyst cites `rlhf_c04` for the constitutional AI claim
  (wrong chunk — that chunk is about overoptimization metrics).

## Corpus notes

Five chunks from four synthetic sources covering: reward hacking mechanisms, scalable
oversight proposals, constitutional AI mechanics, overoptimization measurement, and
process reward models. The last two are deliberately made `required: false` — an
analysis that surfaces the top three findings should pass even without the technical
details.

## When to update

If the analyst prompt is updated to produce longer summaries and the expected
insights should be refined accordingly, update `expected_analysis.insights` and note
the reason here. Do not loosen `required: true` without team discussion.
