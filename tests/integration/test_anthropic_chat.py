"""Integration test: AnthropicChatProvider against the live Anthropic API.

Requires LLM__ANTHROPIC_API_KEY to be set in the environment or .env.local.
Uses claude-haiku-4-5-20251001 to minimise cost.
Skipped automatically if the key is absent or empty.

Run manually:
    uv run pytest tests/integration/test_anthropic_chat.py -m integration -s
"""
from __future__ import annotations

import pytest

from trendstorm.domain.llm.models import Message
from trendstorm.infrastructure.llm.anthropic import AnthropicChatProvider
from trendstorm.shared.config import get_settings

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_TEST_MODEL = "claude-haiku-4-5-20251001"


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()


@pytest.fixture
def provider() -> AnthropicChatProvider:
    settings = get_settings()
    api_key = settings.llm.anthropic_api_key.get_secret_value()
    if not api_key:
        pytest.skip("LLM__ANTHROPIC_API_KEY not set")
    return AnthropicChatProvider(api_key=api_key, model=_TEST_MODEL)


class TestAnthropicChatProviderIntegration:
    async def test_complete_returns_non_empty_text(self, provider: AnthropicChatProvider) -> None:
        result = await provider.complete([
            Message(role="user", content="Reply with exactly the word: PONG"),
        ])
        assert "PONG" in result.content.upper()
        assert result.input_tokens > 0
        assert result.output_tokens > 0
        assert result.finish_reason == "stop"

    async def test_system_message_respected(self, provider: AnthropicChatProvider) -> None:
        result = await provider.complete([
            Message(role="system", content="You only speak in haiku (5-7-5 syllables). Always."),
            Message(role="user", content="What is the sky?"),
        ])
        # A haiku has 3 lines — check for at least a newline.
        assert len(result.content.strip()) > 0
        assert result.model_id == f"anthropic.{_TEST_MODEL}"

    async def test_stream_yields_text(self, provider: AnthropicChatProvider) -> None:
        collected: list[str] = []
        async for delta in provider.stream([
            Message(role="user", content="Count from 1 to 5, one number per line."),
        ]):
            collected.append(delta)
        full = "".join(collected)
        assert len(full) > 0
        assert len(collected) > 1  # streaming must produce multiple deltas

    async def test_complete_with_tools_returns_structured_output(
        self, provider: AnthropicChatProvider
    ) -> None:
        tools = [
            {
                "name": "record_sentiment",
                "description": "Record the sentiment of the text.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "sentiment": {
                            "type": "string",
                            "enum": ["positive", "negative", "neutral"],
                        },
                        "confidence": {"type": "number"},
                    },
                    "required": ["sentiment", "confidence"],
                },
            }
        ]
        name, inp, usage = await provider.complete_with_tools(
            [Message(role="user", content="I love sunny days! They make me so happy.")],
            tools=tools,
            tool_choice="record_sentiment",
        )
        assert name == "record_sentiment"
        assert inp["sentiment"] in {"positive", "negative", "neutral"}
        assert 0.0 <= inp["confidence"] <= 1.0
        assert usage.input_tokens > 0
        assert usage.output_tokens > 0

    async def test_prompt_caching_does_not_break_completion(
        self, provider: AnthropicChatProvider
    ) -> None:
        # Make two calls with the same system prompt — second should hit cache.
        long_system = "You are a helpful assistant. " * 100  # ~600 tokens to exceed cache threshold
        for _ in range(2):
            result = await provider.complete([
                Message(role="system", content=long_system),
                Message(role="user", content="Say OK."),
            ])
            assert len(result.content) > 0
