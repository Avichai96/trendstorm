"""Tiktoken wrapper for token counting and text truncation.

Uses cl100k_base universally for all chunking decisions — the same BPE
vocabulary as GPT-4 and OpenAI embeddings. This gives consistent token counts
regardless of which embedding provider is active.

Note: token counts returned here are for chunking/sizing decisions only.
Actual prompt_tokens billed by a provider (e.g. Gemini) may differ slightly
because those providers use their own tokenizers internally. The discrepancy
is small enough to be irrelevant for chunk size decisions.

The encoding is loaded once and cached — tiktoken downloads a vocabulary
file on first call (cached to disk afterwards). Subsequent calls are fast.
"""
from __future__ import annotations

import functools

import tiktoken

ENCODING_NAME = "cl100k_base"


@functools.lru_cache(maxsize=1)
def _get_encoding() -> tiktoken.Encoding:
    """Return the cached cl100k_base encoding. Loaded once per process."""
    return tiktoken.get_encoding(ENCODING_NAME)


def count_tokens(text: str) -> int:
    """Return the number of cl100k_base tokens in text."""
    return len(_get_encoding().encode(text))


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Return text truncated to at most max_tokens tokens.

    Decodes the truncated token sequence back to a string. The result may end
    mid-word if the token boundary falls there — callers that need clean word
    breaks should post-process or use the chunker's overlap logic instead.
    Returns an empty string when max_tokens == 0.
    """
    if max_tokens <= 0:
        return ""
    enc = _get_encoding()
    tokens = enc.encode(text)
    if len(tokens) <= max_tokens:
        return text
    return enc.decode(tokens[:max_tokens])
