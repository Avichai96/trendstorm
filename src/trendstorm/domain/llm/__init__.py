"""LLM domain package — Protocols and value objects for LLM providers."""
from __future__ import annotations

from trendstorm.domain.llm.errors import (
    LLMError,
    LLMPermanentError,
    LLMRateLimitError,
    LLMSchemaError,
    LLMTimeoutError,
    LLMTransientError,
)
from trendstorm.domain.llm.models import Completion, EmbeddingBatchResult, Message
from trendstorm.domain.llm.providers import ChatProvider, EmbeddingProvider

__all__ = [
    "ChatProvider",
    "Completion",
    "EmbeddingBatchResult",
    "EmbeddingProvider",
    "LLMError",
    "LLMPermanentError",
    "LLMRateLimitError",
    "LLMSchemaError",
    "LLMTimeoutError",
    "LLMTransientError",
    "Message",
]
