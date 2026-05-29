"""Anthropic Claude chat provider.

Supports:
    complete()            — single-turn non-streaming completion
    stream()              — async generator of text deltas
    complete_with_tools() — structured output via Anthropic tool-use (NOT on base Protocol)

Prompt caching:
    System messages get cache_control: {"type": "ephemeral"} by default so the
    system prompt and category brief are served from cache after the first call.
    Retrieved chunks are passed in user messages and are NOT cached — they change
    per request. Set cache_system_prompt=False to disable.

Tool use:
    The Analyst calls complete_with_tools() with the Analysis JSON schema as a
    single Anthropic tool definition. Forcing tool_choice to that tool's name
    guarantees the model returns schema-validated JSON, not prose. The validator
    uses the same mechanism with its own schema.
    This method is NOT part of the ChatProvider Protocol — it is Anthropic-specific.

Error hierarchy:
    RateLimitError         → LLMRateLimitError  (transient, retried)
    APITimeoutError        → LLMTimeoutError    (transient, retried)
    AuthenticationError    → LLMPermanentError
    PermissionDeniedError  → LLMPermanentError
    BadRequestError        → LLMPermanentError
    InternalServerError    → LLMTransientError
    APIConnectionError     → LLMTransientError
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, NoReturn

from trendstorm.domain.llm.errors import (
    LLMPermanentError,
    LLMRateLimitError,
    LLMSchemaError,
    LLMTimeoutError,
    LLMTransientError,
)
from trendstorm.domain.llm.models import Completion, Message, TokenUsage
from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = get_logger(__name__)

_DEFAULT_MODEL = "claude-sonnet-4-6"
_DEFAULT_MAX_TOKENS = 8192

_FINISH_REASON_MAP: dict[str, Literal["stop", "length", "content_filter", "tool_calls"]] = {
    "end_turn": "stop",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
}


def _map_anthropic_error(exc: Exception) -> NoReturn:
    """Map Anthropic SDK exceptions to domain LLM errors. Always raises."""
    try:
        import anthropic

        if isinstance(exc, anthropic.RateLimitError):
            raise LLMRateLimitError(str(exc)) from exc
        if isinstance(exc, anthropic.APITimeoutError):
            raise LLMTimeoutError(str(exc)) from exc
        if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
            raise LLMPermanentError(str(exc)) from exc
        if isinstance(exc, anthropic.BadRequestError):
            raise LLMPermanentError(str(exc)) from exc
        if isinstance(exc, anthropic.InternalServerError):
            raise LLMTransientError(str(exc)) from exc
        if isinstance(exc, anthropic.APIConnectionError):
            raise LLMTransientError(str(exc)) from exc
    except ImportError:
        pass

    # Fallback heuristics when we can't import anthropic.
    msg = str(exc).lower()
    if "429" in msg or "rate" in msg:
        raise LLMRateLimitError(str(exc)) from exc
    if "timeout" in msg:
        raise LLMTimeoutError(str(exc)) from exc
    if "401" in msg or "403" in msg or "auth" in msg:
        raise LLMPermanentError(str(exc)) from exc
    raise LLMTransientError(str(exc)) from exc


class AnthropicChatProvider:
    """ChatProvider backed by Anthropic Claude (AsyncAnthropic).

    Satisfies the ChatProvider Protocol for complete() and stream().
    complete_with_tools() is Anthropic-specific — not on the Protocol.

    Args:
        api_key              — Anthropic API key.
        model                — Model identifier (default: claude-sonnet-4-6).
        max_tokens           — Upper bound on completion length.
        cache_system_prompt  — Attach cache_control to system messages for prompt caching.
        temperature          — Sampling temperature. None = use model default.
        _client              — Inject a fake AsyncAnthropic client for unit tests.

    """

    def __init__(
        self,
        api_key: str,
        model: str = _DEFAULT_MODEL,
        *,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        cache_system_prompt: bool = True,
        temperature: float | None = None,
        _client: Any = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._max_tokens = max_tokens
        self._cache_system_prompt = cache_system_prompt
        self._temperature = temperature

        if _client is not None:
            self._client = _client
        else:
            import anthropic  # deferred — avoids hard dep when using test injection

            self._client = anthropic.AsyncAnthropic(api_key=api_key)

    # ------------------------------------------------------------------
    # ChatProvider Protocol
    # ------------------------------------------------------------------

    @property
    def model_id(self) -> str:
        return f"anthropic.{self._model}"

    async def complete(self, messages: list[Message]) -> Completion:
        """Return a single non-streaming completion."""
        system, conv_messages = self._split_system(messages)
        kwargs = self._base_kwargs(system, conv_messages)

        logger.debug("anthropic.complete", model=self._model, n_messages=len(conv_messages))

        try:
            response = await self._client.messages.create(**kwargs)
        except (LLMRateLimitError, LLMTimeoutError, LLMPermanentError, LLMTransientError):
            raise
        except Exception as exc:
            _map_anthropic_error(exc)

        text = self._extract_text(response)
        return Completion(
            content=text,
            model_id=self.model_id,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            finish_reason=_FINISH_REASON_MAP.get(response.stop_reason or ""),
        )

    async def stream(self, messages: list[Message]) -> AsyncIterator[str]:
        """Async generator yielding text deltas as they arrive."""
        system, conv_messages = self._split_system(messages)
        kwargs = self._base_kwargs(system, conv_messages)

        logger.debug("anthropic.stream", model=self._model, n_messages=len(conv_messages))

        try:
            async with self._client.messages.stream(**kwargs) as s:
                async for text in s.text_stream:
                    yield text
        except (LLMRateLimitError, LLMTimeoutError, LLMPermanentError, LLMTransientError):
            raise
        except Exception as exc:
            _map_anthropic_error(exc)

    # ------------------------------------------------------------------
    # Anthropic-specific: structured output via tool use
    # ------------------------------------------------------------------

    async def complete_with_tools(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        tool_choice: str | None = None,
    ) -> tuple[str, dict[str, Any], TokenUsage]:
        """Force a tool-use completion; return (tool_name, tool_input, token_usage).

        Args:
            messages:     Conversation history; the system message is extracted for caching.
            tools:        List of tool definitions in Anthropic schema format.
            tool_choice:  Name of the tool to force. If None, the model chooses freely.

        Returns:
            (tool_name, tool_input, token_usage) where tool_input is the schema-validated
            dict extracted from the tool_use block, and token_usage carries token counts
            for cost attribution (cached_tokens reflects Anthropic prompt-cache reads).

        Raises:
            LLMSchemaError   — if the response contains no tool_use block.
            LLMPermanentError / LLMTransientError — on SDK errors.

        """
        system, conv_messages = self._split_system(messages)
        kwargs = self._base_kwargs(system, conv_messages)
        kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = {"type": "tool", "name": tool_choice}

        logger.debug(
            "anthropic.complete_with_tools",
            model=self._model,
            tools=[t.get("name") for t in tools],
        )

        try:
            response = await self._client.messages.create(**kwargs)
        except (LLMRateLimitError, LLMTimeoutError, LLMPermanentError, LLMTransientError):
            raise
        except Exception as exc:
            _map_anthropic_error(exc)

        usage = TokenUsage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cached_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
        )

        for block in response.content:
            if getattr(block, "type", None) == "tool_use":
                return block.name, dict(block.input), usage

        raise LLMSchemaError(
            "Anthropic tool-use response contained no tool_use block",
            context={
                "model": self._model,
                "stop_reason": response.stop_reason,
                "content_types": [getattr(b, "type", "?") for b in response.content],
            },
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _split_system(
        self, messages: list[Message]
    ) -> tuple[list[dict[str, Any]] | str | None, list[dict[str, Any]]]:
        """Separate the system message and format remaining messages for the API.

        System message with cache_control gets a list-of-blocks format so
        Anthropic's prompt cache can serve it after the first call.
        """
        system_text: str | None = None
        conv: list[dict[str, Any]] = []

        for msg in messages:
            if msg.role == "system":
                system_text = msg.content
            else:
                conv.append({"role": msg.role, "content": msg.content})

        if system_text is None:
            return None, conv

        if self._cache_system_prompt:
            system: list[dict[str, Any]] | str = [
                {
                    "type": "text",
                    "text": system_text,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        else:
            system = system_text

        return system, conv

    def _base_kwargs(
        self,
        system: list[dict[str, Any]] | str | None,
        conv_messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": conv_messages,
        }
        if system is not None:
            kwargs["system"] = system
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature
        return kwargs

    @staticmethod
    def _extract_text(response: Any) -> str:
        """Concatenate all text blocks from a messages response."""
        parts: list[str] = []
        for block in response.content:
            if getattr(block, "type", None) == "text":
                parts.append(block.text)
        return "".join(parts)
