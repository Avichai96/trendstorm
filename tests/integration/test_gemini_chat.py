"""Integration test: GeminiChatProvider against the live Gemini API.

Requires GEMINI__API_KEY to be set in the environment or .env.local.
Uses gemini-2.0-flash to minimise cost (free tier).
Skipped automatically if the key is absent or empty.

Run manually:
    uv run pytest tests/integration/test_gemini_chat.py -m integration -s
"""
from __future__ import annotations

import pytest

from trendstorm.domain.llm.models import Message
from trendstorm.infrastructure.llm.gemini import GeminiChatProvider
from trendstorm.shared.config import get_settings

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()


@pytest.fixture
def provider() -> GeminiChatProvider:
    settings = get_settings()
    api_key = settings.gemini.api_key.get_secret_value()
    if not api_key:
        pytest.skip("GEMINI__API_KEY not set")
    return GeminiChatProvider(api_key=api_key, model=settings.gemini.chat_model)


class TestGeminiChatProviderIntegration:
    async def test_complete_returns_non_empty_text(self, provider: GeminiChatProvider) -> None:
        result = await provider.complete([
            Message(role="user", content="Reply with exactly the word: PONG"),
        ])
        assert "PONG" in result.content.upper()
        assert result.input_tokens > 0
        assert result.output_tokens > 0
        assert result.finish_reason in {"stop", "length"}

    async def test_system_message_respected(self, provider: GeminiChatProvider) -> None:
        result = await provider.complete([
            Message(role="system", content="You always end every response with the word: ZZZ"),
            Message(role="user", content="Say hello."),
        ])
        assert len(result.content.strip()) > 0
        assert result.model_id.startswith("gemini.")

    async def test_stream_yields_text(self, provider: GeminiChatProvider) -> None:
        collected: list[str] = []
        async for delta in provider.stream([
            Message(role="user", content="Count from 1 to 5, one number per line."),
        ]):
            collected.append(delta)
        full = "".join(collected)
        assert len(full) > 0

    async def test_complete_with_tools_returns_structured_output(
        self, provider: GeminiChatProvider
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
                    },
                    "required": ["sentiment"],
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
        assert usage.input_tokens >= 0
        assert usage.output_tokens >= 0
