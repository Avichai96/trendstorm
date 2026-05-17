"""Provider Protocols for LLM interactions.

Two Protocols, never combined:
    EmbeddingProvider — batch text → vectors only.
    ChatProvider      — chat completion + token streaming only.

Why separate?
    Embedding and chat have different rate limits, billing, latency, and
    model families. A provider may implement only one. Retry wrappers,
    routing logic, and cost telemetry can target each independently.
    Never merge them into a single "LLMProvider" interface.

model_id format: "{provider}.{model_name}"
    e.g. "gemini.text-embedding-004", "openai.gpt-4o-mini"
    Used by the vector store to name Chroma collections; swapping providers
    creates a new collection rather than silently mixing incompatible vectors.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from trendstorm.domain.llm.models import Completion, EmbeddingBatchResult, Message, TokenUsage


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Stateless interface for converting text into dense vectors."""

    @property
    def model_id(self) -> str:
        """Canonical '{provider}.{model_name}' — part of the Chroma collection name."""
        ...

    @property
    def dimensions(self) -> int:
        """Output vector dimensionality. Fixed for a given model."""
        ...

    @property
    def max_batch_size(self) -> int:
        """Maximum number of texts per embed_batch call."""
        ...

    @property
    def max_input_tokens(self) -> int:
        """Maximum tokens for a single input text. Chunker must enforce this."""
        ...

    async def embed_batch(
        self,
        texts: list[str],
        *,
        task_type: Literal["document", "query"] = "document",
    ) -> EmbeddingBatchResult:
        """Embed texts. len(result.vectors) == len(texts), same order.

        task_type distinguishes retrieval roles:
            "document" — embedding content to be stored (default, backward compatible).
            "query"    — embedding a search query; some providers use different
                         model weights for this (e.g. Gemini's RETRIEVAL_QUERY).
        Providers that do not support asymmetric embeddings accept and ignore it.
        """
        ...


@runtime_checkable
class ChatProvider(Protocol):
    """Stateless interface for chat completion and streaming."""

    @property
    def model_id(self) -> str:
        """Canonical '{provider}.{model_name}'."""
        ...

    async def complete(self, messages: list[Message]) -> Completion:
        """Return a single non-streaming completion."""
        ...

    def stream(self, messages: list[Message]) -> AsyncIterator[str]:
        """Yield text deltas as they arrive. Use `async for` to consume."""
        ...


@runtime_checkable
class StructuredChatProvider(Protocol):
    """Chat provider that supports tool-use / structured output.

    All three production providers (Anthropic, Gemini, OpenAI) satisfy this
    Protocol via complete_with_tools(). Services that need schema-validated
    output (Analyst, Validator) depend on this Protocol so the choice of
    backend is configurable without changing service code.

    Tool definitions use the Anthropic-style format — each provider adapts
    internally:
        [{"name": "...", "description": "...", "input_schema": {...JSON Schema...}}]
    """

    @property
    def model_id(self) -> str:
        """Canonical '{provider}.{model_name}'."""
        ...

    async def complete_with_tools(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        tool_choice: str | None = None,
    ) -> tuple[str, dict[str, Any], TokenUsage]:
        """Force a tool-use completion.

        Returns (tool_name, tool_input, token_usage) where tool_input is the
        schema-validated dict the model supplied for the named tool.
        token_usage carries input/output/cached counts for cost attribution.
        tool_choice forces a specific tool name; None lets the model choose.

        Raises LLMSchemaError if the model returns no tool call.
        """
        ...
