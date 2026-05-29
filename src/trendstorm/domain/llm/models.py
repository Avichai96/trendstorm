"""Value objects exchanged between callers and LLM providers."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Message(BaseModel):
    """Single turn in a chat conversation."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant", "system"]
    content: str = Field(..., min_length=1)


class Completion(BaseModel):
    """Result of a chat completion call."""

    model_config = ConfigDict(extra="forbid")

    content: str
    # Canonical "{provider}.{model_name}", e.g. "gemini.gemini-2.0-flash".
    model_id: str
    input_tokens: int = Field(..., ge=0)
    output_tokens: int = Field(..., ge=0)
    finish_reason: Literal["stop", "length", "content_filter", "tool_calls"] | None = None


class TokenUsage(BaseModel):
    """Token counts returned by a single LLM API call.

    Returned alongside (tool_name, tool_input) by complete_with_tools() so
    callers can persist counts for cost attribution without a separate query.
    cached_tokens: Anthropic prompt-cache read tokens (subset of input_tokens).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int = Field(..., ge=0)
    output_tokens: int = Field(..., ge=0)
    cached_tokens: int = Field(default=0, ge=0)


class EmbeddingBatchResult(BaseModel):
    """Result of a batch embedding call.

    vectors[i] is the dense embedding for texts[i] in the originating call.
    model_id uses the canonical "{provider}.{model_name}" format, e.g.
    "gemini.text-embedding-004", which is also used to name Chroma collections.
    Mixing vectors from different model_ids in the same collection is forbidden.
    """

    model_config = ConfigDict(extra="forbid")

    vectors: list[list[float]]
    input_tokens: int = Field(..., ge=0)  # total tokens consumed across the batch
    model_id: str
