"""Provider factories — construct EmbeddingProvider and ChatProvider from settings.

build_embedding_provider — wraps embedding provider with retry.
build_chat_provider      — no retry wrapper (chat errors are surfaced immediately;
                           retry is the caller's responsibility for structured tasks).

Extending: add a new LLMProvider enum value, a new case here, and a new
concrete provider class. Nothing else needs to change.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from trendstorm.infrastructure.llm.anthropic import AnthropicChatProvider
from trendstorm.infrastructure.llm.gemini import GeminiChatProvider, GeminiEmbeddingProvider
from trendstorm.infrastructure.llm.ollama import OllamaEmbeddingProvider
from trendstorm.infrastructure.llm.openai import OpenAIChatProvider, OpenAIEmbeddingProvider
from trendstorm.infrastructure.llm.retry import with_retry
from trendstorm.shared.config import LLMProvider
from trendstorm.shared.errors import ConfigError

if TYPE_CHECKING:
    from trendstorm.domain.llm.providers import (
        EmbeddingProvider,
        StructuredChatProvider,
    )
    from trendstorm.shared.config import Settings


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    """Return a retry-wrapped EmbeddingProvider selected from settings.

    Raises ConfigError for unsupported providers (e.g. ANTHROPIC — no
    embedding model in that family).
    """
    provider_type = settings.llm.default_embedding_provider

    match provider_type:
        case LLMProvider.GEMINI:
            base: EmbeddingProvider = GeminiEmbeddingProvider(
                api_key=settings.gemini.api_key.get_secret_value(),
                model=settings.gemini.embedding_model,
            )
        case LLMProvider.OPENAI:
            base = OpenAIEmbeddingProvider(
                api_key=settings.llm.openai_api_key.get_secret_value(),
                model=settings.llm.openai_embedding_model,
            )
        case LLMProvider.OLLAMA:
            base = OllamaEmbeddingProvider(
                host=settings.llm.ollama_base_url,
                model=settings.llm.ollama_embedding_model,
            )
        case _:
            raise ConfigError(
                f"Unsupported embedding provider: {provider_type!r}. "
                "Supported values: gemini, openai, ollama.",
                context={"provider": str(provider_type)},
            )

    return with_retry(
        base,
        max_attempts=settings.llm.retry_max_attempts,
        base_delay=settings.llm.retry_base_delay_seconds,
        max_delay=settings.llm.retry_max_delay_seconds,
    )


def build_chat_provider(settings: Settings) -> StructuredChatProvider:
    """Return a ChatProvider selected by settings.llm.default_chat_provider.

    No retry wrapper — chat providers are used for multi-step tasks (analyst,
    validator, query expansion) where the caller controls retry semantics.

    Raises ConfigError for unsupported providers.
    """
    provider_type = settings.llm.default_chat_provider

    match provider_type:
        case LLMProvider.GEMINI:
            return GeminiChatProvider(
                api_key=settings.gemini.api_key.get_secret_value(),
                model=settings.gemini.chat_model,
            )
        case LLMProvider.ANTHROPIC:
            return AnthropicChatProvider(
                api_key=settings.llm.anthropic_api_key.get_secret_value(),
                model=settings.llm.anthropic_model,
            )
        case LLMProvider.OPENAI:
            return OpenAIChatProvider(
                api_key=settings.llm.openai_api_key.get_secret_value(),
                model=settings.llm.openai_chat_model,
            )
        case _:
            raise ConfigError(
                f"Unsupported chat provider: {provider_type!r}. "
                "Supported values: gemini, anthropic, openai.",
                context={"provider": str(provider_type)},
            )
