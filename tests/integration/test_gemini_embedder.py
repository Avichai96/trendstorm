"""Integration test: GeminiEmbeddingProvider against the live Gemini API.

Requires GEMINI__API_KEY in .env.local (or environment).
Skipped automatically when the key is absent — no failures in CI without credentials.

Run manually after setting the key:
    uv run pytest tests/integration/test_gemini_embedder.py -m integration -s
"""
from __future__ import annotations

import pytest

from trendstorm.infrastructure.llm.gemini import GeminiEmbeddingProvider
from trendstorm.shared.config import get_settings

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()


@pytest.fixture
def api_key() -> str:
    key = get_settings().gemini.api_key.get_secret_value()
    if not key:
        pytest.skip("GEMINI__API_KEY not set — skipping live Gemini API test")
    return key


async def test_embed_batch_live(api_key: str) -> None:
    """Single text → correct vector shape and model_id."""
    provider = GeminiEmbeddingProvider(api_key=api_key)
    texts = ["TrendStorm is an autonomous trend intelligence platform."]

    result = await provider.embed_batch(texts)

    assert len(result.vectors) == 1
    assert len(result.vectors[0]) == provider.dimensions  # 768
    assert result.model_id == "gemini.text-embedding-004"
    assert result.input_tokens >= 0
    # Sanity: values are floats; text-embedding-004 is L2-normalised → |v| ≈ 1
    v = result.vectors[0]
    norm_sq = sum(x * x for x in v)
    assert 0.98 <= norm_sq <= 1.02, f"Expected unit vector, got |v|²={norm_sq:.4f}"


async def test_embed_batch_multi_live(api_key: str) -> None:
    """Multiple texts → one vector per text, all same dimensionality."""
    provider = GeminiEmbeddingProvider(api_key=api_key)
    texts = [
        "Artificial intelligence is transforming industries.",
        "Climate change requires urgent global action.",
        "Quantum computing promises exponential speedups.",
    ]

    result = await provider.embed_batch(texts)

    assert len(result.vectors) == 3
    for vec in result.vectors:
        assert len(vec) == provider.dimensions


async def test_embed_batch_empty_live(api_key: str) -> None:
    """Empty batch returns empty result without hitting the API."""
    provider = GeminiEmbeddingProvider(api_key=api_key)
    result = await provider.embed_batch([])
    assert result.vectors == []
    assert result.input_tokens == 0
