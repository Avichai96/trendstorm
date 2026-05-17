"""Unit tests for GeminiEmbeddingProvider using an injected fake client.

No real API calls; google-genai is not imported (provider receives _client).
"""
from __future__ import annotations

from typing import Any

import pytest

from trendstorm.domain.llm.errors import (
    LLMPermanentError,
    LLMRateLimitError,
    LLMTransientError,
)
from trendstorm.infrastructure.llm.gemini import GeminiEmbeddingProvider

# ---------------------------------------------------------------------------
# Fake Gemini client — mirrors the SDK's response structure
# ---------------------------------------------------------------------------


class _FakeEmbedding:
    def __init__(self, n_dims: int, seed: int = 0) -> None:
        self.values = [round((seed + i) * 0.01, 4) for i in range(n_dims)]


class _FakeEmbedResponse:
    def __init__(self, texts: list[str], n_dims: int) -> None:
        self.embeddings = [_FakeEmbedding(n_dims, seed=i) for i, _ in enumerate(texts)]


class _FakeGeminiModels:
    def __init__(self, n_dims: int = 4) -> None:
        self._n_dims = n_dims

    def embed_content(
        self,
        model: str,
        contents: list[str],
        config: Any = None,
    ) -> _FakeEmbedResponse:
        return _FakeEmbedResponse(contents, self._n_dims)


class _FakeGeminiClient:
    def __init__(self, n_dims: int = 4) -> None:
        self.models = _FakeGeminiModels(n_dims)


# Fake clients that raise on embed_content
class _ErrorModels:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def embed_content(self, **kwargs: Any) -> Any:
        raise self._exc


class _ErrorClient:
    def __init__(self, exc: Exception) -> None:
        self.models = _ErrorModels(exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider(n_dims: int = 4) -> GeminiEmbeddingProvider:
    return GeminiEmbeddingProvider(api_key="fake", _client=_FakeGeminiClient(n_dims))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGeminiEmbeddingProviderProperties:
    def test_model_id_format(self) -> None:
        p = _make_provider()
        assert p.model_id == "gemini.text-embedding-004"

    def test_custom_model_name(self) -> None:
        p = GeminiEmbeddingProvider(
            api_key="x", model="text-embedding-005", _client=_FakeGeminiClient()
        )
        assert p.model_id == "gemini.text-embedding-005"

    def test_default_dimensions(self) -> None:
        p = GeminiEmbeddingProvider(api_key="x", _client=_FakeGeminiClient())
        assert p.dimensions == 768

    def test_custom_dimensions(self) -> None:
        p = GeminiEmbeddingProvider(
            api_key="x", output_dimensionality=256, _client=_FakeGeminiClient(n_dims=256)
        )
        assert p.dimensions == 256

    def test_max_batch_size(self) -> None:
        assert _make_provider().max_batch_size == 100

    def test_max_input_tokens(self) -> None:
        assert _make_provider().max_input_tokens == 2048

    def test_satisfies_embedding_provider_protocol(self) -> None:
        from trendstorm.domain.llm.providers import EmbeddingProvider

        assert isinstance(_make_provider(), EmbeddingProvider)


@pytest.mark.unit
class TestGeminiEmbedBatch:
    async def test_returns_correct_vector_count(self) -> None:
        p = _make_provider(n_dims=4)
        result = await p.embed_batch(["hello", "world", "test"])
        assert len(result.vectors) == 3

    async def test_vector_length_matches_dimensions(self) -> None:
        p = _make_provider(n_dims=4)
        result = await p.embed_batch(["text one"])
        assert len(result.vectors[0]) == 4

    async def test_empty_batch_returns_empty_no_api_call(self) -> None:
        p = _make_provider()
        result = await p.embed_batch([])
        assert result.vectors == []
        assert result.input_tokens == 0
        assert result.model_id == "gemini.text-embedding-004"

    async def test_model_id_in_result(self) -> None:
        p = _make_provider()
        result = await p.embed_batch(["x"])
        assert result.model_id == "gemini.text-embedding-004"

    async def test_input_tokens_estimated(self) -> None:
        p = _make_provider()
        result = await p.embed_batch(["hello world", "one two three"])
        # 2 + 3 words = 5 estimated tokens
        assert result.input_tokens == 5

    async def test_vectors_are_floats(self) -> None:
        p = _make_provider(n_dims=4)
        result = await p.embed_batch(["some text"])
        for v in result.vectors[0]:
            assert isinstance(v, float)


@pytest.mark.unit
class TestGeminiErrorMapping:
    async def test_rate_limit_from_message(self) -> None:
        p = GeminiEmbeddingProvider(
            api_key="x", _client=_ErrorClient(Exception("HTTP 429 rate limit exceeded"))
        )
        with pytest.raises(LLMRateLimitError):
            await p.embed_batch(["text"])

    async def test_quota_maps_to_rate_limit(self) -> None:
        p = GeminiEmbeddingProvider(
            api_key="x", _client=_ErrorClient(Exception("quota exceeded for project"))
        )
        with pytest.raises(LLMRateLimitError):
            await p.embed_batch(["text"])

    async def test_auth_error_maps_to_permanent(self) -> None:
        p = GeminiEmbeddingProvider(
            api_key="x", _client=_ErrorClient(Exception("401 Unauthorized invalid auth"))
        )
        with pytest.raises(LLMPermanentError):
            await p.embed_batch(["text"])

    async def test_generic_error_maps_to_transient(self) -> None:
        p = GeminiEmbeddingProvider(
            api_key="x", _client=_ErrorClient(Exception("connection refused"))
        )
        with pytest.raises(LLMTransientError):
            await p.embed_batch(["text"])

    async def test_domain_errors_pass_through(self) -> None:
        """LLM domain errors raised by client are not re-wrapped."""
        p = GeminiEmbeddingProvider(
            api_key="x", _client=_ErrorClient(LLMRateLimitError("already mapped"))
        )
        with pytest.raises(LLMRateLimitError):
            await p.embed_batch(["text"])
