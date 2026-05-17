"""Unit tests for OpenAIEmbeddingProvider using an injected fake async client.

No real API calls are made. OpenAI exception instances are constructed with
real httpx objects (httpx is already a project dependency).
"""
from __future__ import annotations

from typing import Any

import httpx
import openai
import pytest

from trendstorm.domain.llm.errors import (
    LLMPermanentError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMTransientError,
)
from trendstorm.infrastructure.llm.openai import OpenAIEmbeddingProvider

# ---------------------------------------------------------------------------
# Fake async OpenAI client — mirrors the SDK's async response structure
# ---------------------------------------------------------------------------

_API_URL = "https://api.openai.com/v1/embeddings"


class _FakeEmbeddingItem:
    def __init__(self, n_dims: int, seed: int = 0) -> None:
        self.embedding = [round((seed + i) * 0.01, 4) for i in range(n_dims)]


class _FakeUsage:
    def __init__(self, tokens: int) -> None:
        self.prompt_tokens = tokens


class _FakeEmbeddingResponse:
    def __init__(self, texts: list[str], n_dims: int) -> None:
        self.data = [_FakeEmbeddingItem(n_dims, seed=i) for i, _ in enumerate(texts)]
        self.usage = _FakeUsage(sum(len(t.split()) for t in texts))


class _FakeEmbeddings:
    def __init__(self, n_dims: int = 4) -> None:
        self._n_dims = n_dims

    async def create(
        self,
        model: str,
        input: list[str],
        dimensions: int | None = None,
    ) -> _FakeEmbeddingResponse:
        return _FakeEmbeddingResponse(input, self._n_dims)


class _FakeOpenAIClient:
    def __init__(self, n_dims: int = 4) -> None:
        self.embeddings = _FakeEmbeddings(n_dims)


class _ErrorEmbeddings:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def create(self, **kwargs: Any) -> Any:
        raise self._exc


class _ErrorClient:
    def __init__(self, exc: Exception) -> None:
        self.embeddings = _ErrorEmbeddings(exc)


# ---------------------------------------------------------------------------
# Helpers for constructing real OpenAI exception instances
# ---------------------------------------------------------------------------


def _req() -> httpx.Request:
    return httpx.Request("POST", _API_URL)


def _resp(status: int) -> httpx.Response:
    return httpx.Response(status, request=_req())


def _rate_limit_error() -> openai.RateLimitError:
    return openai.RateLimitError("rate limit", response=_resp(429), body=None)


def _timeout_error() -> openai.APITimeoutError:
    return openai.APITimeoutError(request=_req())


def _auth_error() -> openai.AuthenticationError:
    return openai.AuthenticationError("invalid key", response=_resp(401), body=None)


def _bad_request_error() -> openai.BadRequestError:
    return openai.BadRequestError("bad request", response=_resp(400), body=None)


def _server_error() -> openai.InternalServerError:
    return openai.InternalServerError("server error", response=_resp(503), body=None)


def _connection_error() -> openai.APIConnectionError:
    return openai.APIConnectionError(request=_req())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider(n_dims: int = 4) -> OpenAIEmbeddingProvider:
    return OpenAIEmbeddingProvider(api_key="fake", _client=_FakeOpenAIClient(n_dims))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOpenAIEmbeddingProviderProperties:
    def test_model_id_format(self) -> None:
        p = _make_provider()
        assert p.model_id == "openai.text-embedding-3-small"

    def test_custom_model_name(self) -> None:
        p = OpenAIEmbeddingProvider(
            api_key="x",
            model="text-embedding-3-large",
            _client=_FakeOpenAIClient(),
        )
        assert p.model_id == "openai.text-embedding-3-large"

    def test_default_dimensions(self) -> None:
        p = OpenAIEmbeddingProvider(api_key="x", _client=_FakeOpenAIClient())
        assert p.dimensions == 1536

    def test_custom_dimensions(self) -> None:
        p = OpenAIEmbeddingProvider(
            api_key="x", output_dimensionality=512, _client=_FakeOpenAIClient(n_dims=512)
        )
        assert p.dimensions == 512

    def test_max_batch_size(self) -> None:
        assert _make_provider().max_batch_size == 2048

    def test_max_input_tokens(self) -> None:
        assert _make_provider().max_input_tokens == 8191

    def test_satisfies_embedding_provider_protocol(self) -> None:
        from trendstorm.domain.llm.providers import EmbeddingProvider

        assert isinstance(_make_provider(), EmbeddingProvider)


@pytest.mark.unit
class TestOpenAIEmbedBatch:
    async def test_returns_correct_vector_count(self) -> None:
        p = _make_provider(n_dims=4)
        result = await p.embed_batch(["a", "b", "c"])
        assert len(result.vectors) == 3

    async def test_vector_length_matches_dimensions(self) -> None:
        p = _make_provider(n_dims=4)
        result = await p.embed_batch(["hello world"])
        assert len(result.vectors[0]) == 4

    async def test_empty_batch_no_api_call(self) -> None:
        p = _make_provider()
        result = await p.embed_batch([])
        assert result.vectors == []
        assert result.input_tokens == 0

    async def test_model_id_in_result(self) -> None:
        p = _make_provider()
        result = await p.embed_batch(["x"])
        assert result.model_id == "openai.text-embedding-3-small"

    async def test_token_count_from_response(self) -> None:
        # Unlike Gemini, OpenAI returns exact token counts.
        p = _make_provider()
        result = await p.embed_batch(["hello world"])  # 2 words → usage.prompt_tokens=2
        assert result.input_tokens == 2

    async def test_vectors_are_floats(self) -> None:
        p = _make_provider(n_dims=4)
        result = await p.embed_batch(["text"])
        for v in result.vectors[0]:
            assert isinstance(v, float)


@pytest.mark.unit
class TestOpenAIErrorMapping:
    async def test_rate_limit_error(self) -> None:
        p = OpenAIEmbeddingProvider(api_key="x", _client=_ErrorClient(_rate_limit_error()))
        with pytest.raises(LLMRateLimitError):
            await p.embed_batch(["text"])

    async def test_timeout_error(self) -> None:
        p = OpenAIEmbeddingProvider(api_key="x", _client=_ErrorClient(_timeout_error()))
        with pytest.raises(LLMTimeoutError):
            await p.embed_batch(["text"])

    async def test_auth_error_is_permanent(self) -> None:
        p = OpenAIEmbeddingProvider(api_key="x", _client=_ErrorClient(_auth_error()))
        with pytest.raises(LLMPermanentError):
            await p.embed_batch(["text"])

    async def test_bad_request_is_permanent(self) -> None:
        p = OpenAIEmbeddingProvider(api_key="x", _client=_ErrorClient(_bad_request_error()))
        with pytest.raises(LLMPermanentError):
            await p.embed_batch(["text"])

    async def test_server_5xx_is_transient(self) -> None:
        p = OpenAIEmbeddingProvider(api_key="x", _client=_ErrorClient(_server_error()))
        with pytest.raises(LLMTransientError):
            await p.embed_batch(["text"])

    async def test_connection_error_is_transient(self) -> None:
        p = OpenAIEmbeddingProvider(api_key="x", _client=_ErrorClient(_connection_error()))
        with pytest.raises(LLMTransientError):
            await p.embed_batch(["text"])

    async def test_domain_errors_pass_through(self) -> None:
        p = OpenAIEmbeddingProvider(
            api_key="x", _client=_ErrorClient(LLMRateLimitError("already mapped"))
        )
        with pytest.raises(LLMRateLimitError):
            await p.embed_batch(["text"])
