# Golden Example: LLM Interpretability

## Purpose

Tests that the analyst correctly identifies and distinguishes the three primary
mechanistic interpretability techniques (superposition, SAEs, circuit analysis)
from a corpus that also includes feature steering and probing limitations. The
example is designed to be "medium-hard" because the concepts are closely related
and an unfocused analyst might conflate SAEs with circuit analysis or overweight
the probing chunk.

## What failure looks like

- **Coverage failure**: analyst discusses superposition and circuits but omits SAEs
  (all three are required).
- **Faithfulness failure**: analyst claims SAEs can "control" model behavior —
  that claim belongs to feature steering (interp_c04), not SAEs (interp_c02).
- **Citation accuracy failure**: analyst cites `interp_c05` (probing limitations)
  to support a claim about SAE feature quality.

## Corpus notes

Five chunks covering: superposition hypothesis, sparse autoencoders, circuit
analysis via activation patching, feature steering, and probing limitations. The
fifth chunk (probing) is included to test whether the analyst can recognize it as
context rather than a primary finding. The first four chunks are the core story.

## Difficulty rationale

Marked "medium-hard" because the concepts have significant conceptual overlap
(SAEs and circuits both address polysemanticity; feature steering and activation
patching both involve residual stream manipulation). A weaker analyst may conflate
findings across chunks. The citation accuracy evaluator is particularly useful here.
