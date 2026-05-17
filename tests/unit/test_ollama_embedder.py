"""Unit tests for OllamaEmbeddingProvider using an injected fake async client."""
from __future__ import annotations

from typing import Any

import ollama
import pytest

from trendstorm.domain.llm.errors import LLMPermanentError, LLMTransientError
from trendstorm.infrastructure.llm.ollama import OllamaEmbeddingProvider

# ---------------------------------------------------------------------------
# Fake async Ollama client — mirrors EmbedResponse structure
# ---------------------------------------------------------------------------


class _FakeEmbedResponse:
    def __init__(self, texts: list[str], n_dims: int, token_count: int | None = None) -> None:
        self.embeddings = [[round(i * 0.01, 4) for i in range(n_dims)] for _ in texts]
        self.prompt_eval_count = token_count  # None → provider falls back to word estimate


class _FakeOllamaClient:
    def __init__(self, n_dims: int = 4, token_count: int | None = None) -> None:
        self._n_dims = n_dims
        self._token_count = token_count

    async def embed(
        self, model: str, input: list[str], **kwargs: Any
    ) -> _FakeEmbedResponse:
        return _FakeEmbedResponse(input, self._n_dims, self._token_count)


class _ErrorOllamaClient:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def embed(self, **kwargs: Any) -> Any:
        raise self._exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider(n_dims: int = 4) -> OllamaEmbeddingProvider:
    return OllamaEmbeddingProvider(_client=_FakeOllamaClient(n_dims))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOllamaEmbeddingProviderProperties:
    def test_model_id_format(self) -> None:
        assert _make_provider().model_id == "ollama.nomic-embed-text"

    def test_custom_model(self) -> None:
        p = OllamaEmbeddingProvider(model="mxbai-embed-large", _client=_FakeOllamaClient())
        assert p.model_id == "ollama.mxbai-embed-large"

    def test_default_dimensions(self) -> None:
        p = OllamaEmbeddingProvider(_client=_FakeOllamaClient())
        assert p.dimensions == 768

    def test_custom_dimensions(self) -> None:
        p = OllamaEmbeddingProvider(output_dimensionality=1024, _client=_FakeOllamaClient(n_dims=1024))
        assert p.dimensions == 1024

    def test_max_batch_size(self) -> None:
        assert _make_provider().max_batch_size == 64

    def test_max_input_tokens(self) -> None:
        assert _make_provider().max_input_tokens == 8192

    def test_satisfies_embedding_provider_protocol(self) -> None:
        from trendstorm.domain.llm.providers import EmbeddingProvider

        assert isinstance(_make_provider(), EmbeddingProvider)


@pytest.mark.unit
class TestOllamaEmbedBatch:
    async def test_returns_correct_vector_count(self) -> None:
        result = await _make_provider(n_dims=4).embed_batch(["a", "b", "c"])
        assert len(result.vectors) == 3

    async def test_vector_length_matches_dimensions(self) -> None:
        result = await _make_provider(n_dims=4).embed_batch(["hello"])
        assert len(result.vectors[0]) == 4

    async def test_empty_batch_no_api_call(self) -> None:
        result = await _make_provider().embed_batch([])
        assert result.vectors == []
        assert result.input_tokens == 0

    async def test_model_id_in_result(self) -> None:
        result = await _make_provider().embed_batch(["x"])
        assert result.model_id == "ollama.nomic-embed-text"

    async def test_token_count_from_response(self) -> None:
        p = OllamaEmbeddingProvider(_client=_FakeOllamaClient(n_dims=4, token_count=42))
        result = await p.embed_batch(["text"])
        assert result.input_tokens == 42

    async def test_token_count_falls_back_to_estimate_when_none(self) -> None:
        # prompt_eval_count=None → word-count estimate
        p = OllamaEmbeddingProvider(_client=_FakeOllamaClient(n_dims=4, token_count=None))
        result = await p.embed_batch(["hello world"])  # fake sets count=1*5=5, but we set None
        # FakeOllamaClient sets token_count=None → falls back: sum(len(t.split()) for t in texts)
        # Wait — our fake uses None to mean "send None to prompt_eval_count"
        # The provider logic: `response.prompt_eval_count or word_estimate`
        # None is falsy → uses word estimate: len("hello world".split()) = 2
        assert result.input_tokens == 2

    async def test_vectors_are_floats(self) -> None:
        result = await _make_provider(n_dims=4).embed_batch(["text"])
        for v in result.vectors[0]:
            assert isinstance(v, float)


@pytest.mark.unit
class TestOllamaErrorMapping:
    async def test_model_not_found_is_permanent(self) -> None:
        exc = ollama.ResponseError("model 'nomic-embed-text' not found", status_code=404)
        p = OllamaEmbeddingProvider(_client=_ErrorOllamaClient(exc))
        with pytest.raises(LLMPermanentError) as exc_info:
            await p.embed_batch(["text"])
        assert "not found" in str(exc_info.value).lower()

    async def test_response_error_5xx_is_transient(self) -> None:
        exc = ollama.ResponseError("internal server error", status_code=500)
        p = OllamaEmbeddingProvider(_client=_ErrorOllamaClient(exc))
        with pytest.raises(LLMTransientError):
            await p.embed_batch(["text"])

    async def test_request_error_is_permanent(self) -> None:
        exc = ollama.RequestError("malformed request")
        p = OllamaEmbeddingProvider(_client=_ErrorOllamaClient(exc))
        with pytest.raises(LLMPermanentError):
            await p.embed_batch(["text"])

    async def test_connection_refused_is_transient(self) -> None:
        exc = OSError("Connection refused")
        p = OllamaEmbeddingProvider(_client=_ErrorOllamaClient(exc))
        with pytest.raises(LLMTransientError):
            await p.embed_batch(["text"])

    async def test_generic_exception_is_transient(self) -> None:
        p = OllamaEmbeddingProvider(_client=_ErrorOllamaClient(Exception("unexpected")))
        with pytest.raises(LLMTransientError):
            await p.embed_batch(["text"])
