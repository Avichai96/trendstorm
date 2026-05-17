"""Integration test: OpenAIEmbeddingProvider against the live OpenAI API.

Requires LLM__OPENAI_API_KEY in .env.local (or environment).
Skipped automatically when the key is absent.

Run manually:
    uv run pytest tests/integration/test_openai_embedder.py -m integration -s
"""
from __future__ import annotations

import pytest

from trendstorm.infrastructure.llm.openai import OpenAIEmbeddingProvider
from trendstorm.shared.config import get_settings

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()


@pytest.fixture
def api_key() -> str:
    key = get_settings().llm.openai_api_key.get_secret_value()
    if not key:
        pytest.skip("LLM__OPENAI_API_KEY not set — skipping live OpenAI API test")
    return key


async def test_embed_batch_live(api_key: str) -> None:
    """Single text → correct vector shape, real token count, correct model_id."""
    provider = OpenAIEmbeddingProvider(api_key=api_key)
    texts = ["TrendStorm is an autonomous trend intelligence platform."]

    result = await provider.embed_batch(texts)

    assert len(result.vectors) == 1
    assert len(result.vectors[0]) == provider.dimensions  # 1536
    assert result.model_id == "openai.text-embedding-3-small"
    assert result.input_tokens > 0  # real token count from API


async def test_embed_batch_multi_live(api_key: str) -> None:
    """Multiple texts → one vector per text, all same dimensionality."""
    provider = OpenAIEmbeddingProvider(api_key=api_key)
    texts = [
        "Artificial intelligence is transforming industries.",
        "Climate change requires urgent global action.",
    ]

    result = await provider.embed_batch(texts)

    assert len(result.vectors) == 2
    for vec in result.vectors:
        assert len(vec) == provider.dimensions


async def test_embed_batch_empty_live(api_key: str) -> None:
    """Empty batch short-circuits without hitting the API."""
    provider = OpenAIEmbeddingProvider(api_key=api_key)
    result = await provider.embed_batch([])
    assert result.vectors == []
    assert result.input_tokens == 0
