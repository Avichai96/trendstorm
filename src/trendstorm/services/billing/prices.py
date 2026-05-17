"""LLM token price table.

Prices are in micro-dollars (USD x 10^-6) per 1000 tokens. Using integers
avoids floating-point rounding errors in cost accumulation.

Sources (as of 2026-05-25):
  Anthropic: https://www.anthropic.com/pricing
  OpenAI: https://openai.com/pricing
  Google: https://ai.google.dev/pricing
  Cohere: https://cohere.com/pricing

Ollama is local inference with no per-token billing; cost is always 0.

Update this table when provider pricing changes. All workers read from here;
there is no other place where token costs are computed.
"""
from __future__ import annotations

# Micro-dollars per 1000 tokens (USD x 10^-6 / 1k tokens)
_USD_PER_1K = 1_000  # 1 USD = 1_000_000 micro; per 1k = 1_000


def _usd(cents_per_1k: float) -> int:
    """Convert cents-per-1k-tokens to micro-dollars-per-1k-tokens."""
    return int(cents_per_1k * 10_000)   # cents * 10 = milli-dollars * 1000 = micro-dollars


# Table: (provider, model_id) → (input_usd_micro_per_1k, output_usd_micro_per_1k)
# Cached input tokens are billed at 10% of normal input rate (Anthropic-specific).
_PRICE_TABLE: dict[tuple[str, str], tuple[int, int]] = {
    # Anthropic Claude 3.5 Sonnet
    ("anthropic", "claude-sonnet-4-6"):  (_usd(0.3), _usd(1.5)),
    ("anthropic", "claude-opus-4-7"):     (_usd(1.5), _usd(7.5)),
    ("anthropic", "claude-haiku-4-5-20251001"): (_usd(0.08), _usd(0.4)),
    # OpenAI
    ("openai", "gpt-4o"):               (_usd(0.5), _usd(1.5)),
    ("openai", "gpt-4o-mini"):          (_usd(0.015), _usd(0.06)),
    ("openai", "text-embedding-3-small"): (_usd(0.002), 0),
    ("openai", "text-embedding-3-large"): (_usd(0.013), 0),
    # Gemini
    ("gemini", "gemini-2.0-flash"):     (_usd(0.0375), _usd(0.15)),
    ("gemini", "gemini-1.5-pro"):       (_usd(0.35), _usd(1.05)),
    ("gemini", "text-embedding-004"):   (_usd(0.00001), 0),
    # Cohere reranking
    ("cohere", "rerank-v3.5"):          (_usd(0.02), 0),   # per 1k query+doc tokens
    # Ollama — local, no billing
    ("ollama", "*"):                    (0, 0),
}

_FALLBACK = (0, 0)   # unknown model → treat as free rather than error


def compute_cost_usd_micro(
    *,
    provider: str,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
) -> int:
    """Compute cost in micro-dollars for a single LLM call.

    Cached tokens (Anthropic prompt cache) are billed at 10% of input rate.
    """
    # Normalize provider: strip version suffixes for lookup ("ollama/*" wildcard)
    key = (provider, model_id)
    if key not in _PRICE_TABLE and provider == "ollama":
        key = ("ollama", "*")
    input_rate, output_rate = _PRICE_TABLE.get(key, _FALLBACK)

    # Billed input = non-cached portion. Cached billed at 10% of input rate.
    non_cached_input = max(0, input_tokens - cached_tokens)
    cost = (
        (non_cached_input * input_rate) // 1000
        + (cached_tokens * input_rate // 10) // 1000
        + (output_tokens * output_rate) // 1000
    )
    return int(cost)
