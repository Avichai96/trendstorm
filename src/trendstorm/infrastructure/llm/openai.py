"""OpenAI embedding and chat providers.

Uses AsyncOpenAI — fully async, no asyncio.to_thread wrapping needed.
Streaming uses the native async iterator from the SDK.

Embedding (text-embedding-3-small / text-embedding-3-large):
    model_id format: "openai.{model_name}", e.g. "openai.text-embedding-3-small".
    Both models support the `dimensions` parameter for Matryoshka reduction.

Chat (gpt-4o-mini, gpt-4o, etc.):
    Tool use via OpenAI's function-calling API. Accepts the same Anthropic-style
    tool definitions as AnthropicChatProvider for provider-swappable schemas.
    response_format=json_schema is supported via complete_with_json_schema()
    as a strict alternative to tool use (no extra round-trip needed for parsing).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, Literal, NoReturn

import openai

from trendstorm.domain.llm.errors import (
    LLMPermanentError,
    LLMRateLimitError,
    LLMSchemaError,
    LLMTimeoutError,
    LLMTransientError,
)
from trendstorm.domain.llm.models import Completion, EmbeddingBatchResult, Message, TokenUsage
from trendstorm.shared.logging import get_logger

logger = get_logger(__name__)

_OPENAI_MAX_BATCH = 2048
_OPENAI_MAX_INPUT_TOKENS = 8191
_DEFAULT_MODEL = "text-embedding-3-small"
_DEFAULT_DIMENSIONS = 1536
_DEFAULT_CHAT_MODEL = "gpt-4o-mini"
_DEFAULT_CHAT_MAX_TOKENS = 8192

_FINISH_REASON_MAP: dict[str, Literal["stop", "length", "content_filter", "tool_calls"]] = {
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_calls",
    "function_call": "tool_calls",
    "content_filter": "content_filter",
}


def _map_openai_error(exc: Exception) -> NoReturn:
    """Map an OpenAI SDK exception to a domain LLM error. Always raises."""
    if isinstance(exc, openai.RateLimitError):
        raise LLMRateLimitError(str(exc)) from exc
    if isinstance(exc, openai.APITimeoutError):
        raise LLMTimeoutError(str(exc)) from exc
    if isinstance(
        exc, (openai.AuthenticationError, openai.PermissionDeniedError, openai.BadRequestError)
    ):
        raise LLMPermanentError(str(exc)) from exc
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code >= 500:
            raise LLMTransientError(str(exc)) from exc
        raise LLMPermanentError(str(exc)) from exc
    if isinstance(exc, openai.APIConnectionError):
        raise LLMTransientError(str(exc)) from exc
    raise LLMTransientError(str(exc)) from exc


class OpenAIEmbeddingProvider:
    """EmbeddingProvider backed by OpenAI text-embedding-3-small.

    Uses the native AsyncOpenAI client; no thread-pool wrapping needed.
    Token counts come from response.usage.prompt_tokens (exact, not estimated).

    Pass _client to inject a fake for unit tests; leave None for production.
    """

    def __init__(
        self,
        api_key: str,
        model: str = _DEFAULT_MODEL,
        output_dimensionality: int = _DEFAULT_DIMENSIONS,
        *,
        _client: Any = None,
    ) -> None:
        self._model = model
        self._output_dimensionality = output_dimensionality
        self._client = _client if _client is not None else openai.AsyncOpenAI(api_key=api_key)

    # ------------------------------------------------------------------
    # EmbeddingProvider Protocol properties
    # ------------------------------------------------------------------

    @property
    def model_id(self) -> str:
        return f"openai.{self._model}"

    @property
    def dimensions(self) -> int:
        return self._output_dimensionality

    @property
    def max_batch_size(self) -> int:
        return _OPENAI_MAX_BATCH

    @property
    def max_input_tokens(self) -> int:
        return _OPENAI_MAX_INPUT_TOKENS

    # ------------------------------------------------------------------
    # EmbeddingProvider Protocol method
    # ------------------------------------------------------------------

    async def embed_batch(
        self,
        texts: list[str],
        *,
        task_type: Literal["document", "query"] = "document",
    ) -> EmbeddingBatchResult:
        """Embed a batch of texts. len(result.vectors) == len(texts).

        task_type is accepted for Protocol compatibility but ignored — OpenAI's
        text-embedding-3-* models use symmetric embeddings.
        """
        if not texts:
            return EmbeddingBatchResult(vectors=[], input_tokens=0, model_id=self.model_id)

        logger.debug("openai.embed_batch", n_texts=len(texts), model=self._model)

        try:
            response = await self._client.embeddings.create(
                model=self._model,
                input=texts,
                dimensions=self._output_dimensionality,
            )
            vectors = [item.embedding for item in response.data]
            return EmbeddingBatchResult(
                vectors=vectors,
                input_tokens=response.usage.prompt_tokens,
                model_id=self.model_id,
            )
        except (LLMRateLimitError, LLMTimeoutError, LLMPermanentError, LLMTransientError):
            raise
        except Exception as e:
            _map_openai_error(e)


# ===========================================================================
# Chat provider
# ===========================================================================


class OpenAIChatProvider:
    """ChatProvider backed by OpenAI Chat Completions API (AsyncOpenAI).

    Satisfies the ChatProvider Protocol for complete() and stream().
    complete_with_tools() accepts the same Anthropic-style tool definition
    format used by AnthropicChatProvider and GeminiChatProvider — schemas swap
    cleanly across providers.

    Args:
        api_key      — OpenAI API key.
        model        — Chat model name (default: gpt-4o-mini).
        max_tokens   — Upper bound on completion length.
        temperature  — Sampling temperature; None uses model default.
        _client      — Inject a fake AsyncOpenAI client for unit tests.

    """

    def __init__(
        self,
        api_key: str,
        model: str = _DEFAULT_CHAT_MODEL,
        *,
        max_tokens: int = _DEFAULT_CHAT_MAX_TOKENS,
        temperature: float | None = None,
        _client: Any = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._client = _client if _client is not None else openai.AsyncOpenAI(api_key=api_key)

    # ------------------------------------------------------------------
    # ChatProvider Protocol
    # ------------------------------------------------------------------

    @property
    def model_id(self) -> str:
        return f"openai.{self._model}"

    async def complete(self, messages: list[Message]) -> Completion:
        """Return a single non-streaming completion."""
        kwargs = self._base_kwargs(messages)

        logger.debug("openai.complete", model=self._model, n_messages=len(messages))

        try:
            response = await self._client.chat.completions.create(**kwargs)
        except (LLMRateLimitError, LLMTimeoutError, LLMPermanentError, LLMTransientError):
            raise
        except Exception as exc:
            _map_openai_error(exc)

        choice = response.choices[0]
        return Completion(
            content=choice.message.content or "",
            model_id=self.model_id,
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
            finish_reason=_FINISH_REASON_MAP.get(choice.finish_reason or ""),
        )

    async def stream(self, messages: list[Message]) -> AsyncIterator[str]:
        """Async generator yielding content deltas as they arrive."""
        kwargs = self._base_kwargs(messages)
        kwargs["stream"] = True

        logger.debug("openai.stream", model=self._model, n_messages=len(messages))

        try:
            stream = await self._client.chat.completions.create(**kwargs)
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        except (LLMRateLimitError, LLMTimeoutError, LLMPermanentError, LLMTransientError):
            raise
        except Exception as exc:
            _map_openai_error(exc)

    # ------------------------------------------------------------------
    # OpenAI-specific: structured output via function calling
    # ------------------------------------------------------------------

    async def complete_with_tools(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        tool_choice: str | None = None,
    ) -> tuple[str, dict[str, Any], TokenUsage]:
        """Structured output via OpenAI function calling.

        Accepts Anthropic-style tool definitions and converts to OpenAI format.
        Returns (tool_name, tool_input, token_usage) where tool_input is the
        JSON-parsed function arguments from the first tool_call in the response.
        """
        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {}),
                },
            }
            for t in tools
        ]

        kwargs = self._base_kwargs(messages)
        kwargs["tools"] = openai_tools
        if tool_choice is not None:
            kwargs["tool_choice"] = {
                "type": "function",
                "function": {"name": tool_choice},
            }

        logger.debug(
            "openai.complete_with_tools",
            model=self._model,
            tools=[t.get("name") for t in tools],
        )

        try:
            response = await self._client.chat.completions.create(**kwargs)
        except (LLMRateLimitError, LLMTimeoutError, LLMPermanentError, LLMTransientError):
            raise
        except Exception as exc:
            _map_openai_error(exc)

        message = response.choices[0].message
        if not message.tool_calls:
            raise LLMSchemaError(
                "OpenAI tool-call response contained no tool_calls",
                context={
                    "model": self._model,
                    "finish_reason": response.choices[0].finish_reason,
                },
            )

        first = message.tool_calls[0]
        try:
            args = json.loads(first.function.arguments)
        except json.JSONDecodeError as exc:
            raise LLMSchemaError(
                "OpenAI tool_call.function.arguments was not valid JSON",
                context={"error": str(exc), "raw": first.function.arguments[:500]},
            ) from exc

        usage = TokenUsage(
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
        )
        return first.function.name, args, usage

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _base_kwargs(self, messages: list[Message]) -> dict[str, Any]:
        """Build the common kwargs dict for chat.completions.create."""
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature
        return kwargs
