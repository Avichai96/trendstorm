"""Integration test: OllamaEmbeddingProvider against the local Ollama service.

Requires `make up` (starts the Ollama container) and the nomic-embed-text model
to be pulled inside the container. Skipped automatically if Ollama is unreachable.

Run manually:
    uv run pytest tests/integration/test_ollama_embedder.py -m integration -s
"""
from __future__ import annotations

import pytest

from trendstorm.domain.llm.errors import LLMPermanentError
from trendstorm.infrastructure.llm.ollama import OllamaEmbeddingProvider
from trendstorm.shared.config import get_settings

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()


@pytest.fixture
async def provider() -> OllamaEmbeddingProvider:
    """Return a provider connected to the compose Ollama instance.

    Skips the test if Ollama is not reachable.
    """
    settings = get_settings()
    p = OllamaEmbeddingProvider(
        host=settings.llm.ollama_base_url,
        model=settings.llm.ollama_embedding_model,
    )
    # Probe connectivity with a minimal embed call.
    try:
        await p.embed_batch(["ping"])
    except LLMPermanentError:
        # Model not pulled — skip; infrastructure is up but model missing.
        pytest.skip(
            f"Ollama model '{settings.llm.ollama_embedding_model}' not available "
            "— run `ollama pull nomic-embed-text` inside the container"
        )
    except Exception as e:
        pytest.skip(f"Ollama not reachable: {e}")
    return p


async def test_embed_batch_live(provider: OllamaEmbeddingProvider) -> None:
    """Single text → correct vector shape."""
    texts = ["TrendStorm is an autonomous trend intelligence platform."]
    result = await provider.embed_batch(texts)

    assert len(result.vectors) == 1
    assert len(result.vectors[0]) == provider.dimensions
    assert result.model_id == provider.model_id
    assert result.input_tokens >= 0


async def test_embed_batch_multi_live(provider: OllamaEmbeddingProvider) -> None:
    """Multiple texts → one vector per text, consistent dimensionality."""
    texts = [
        "Artificial intelligence is transforming industries.",
        "Climate change requires urgent global action.",
        "Quantum computing promises exponential speedups.",
    ]
    result = await provider.embed_batch(texts)

    assert len(result.vectors) == 3
    for vec in result.vectors:
        assert len(vec) == provider.dimensions


async def test_embed_batch_empty_live(provider: OllamaEmbeddingProvider) -> None:
    """Empty batch short-circuits without hitting the server."""
    result = await provider.embed_batch([])
    assert result.vectors == []
    assert result.input_tokens == 0
