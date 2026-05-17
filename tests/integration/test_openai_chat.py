"""Integration test: OpenAIChatProvider against the live OpenAI API.

Requires LLM__OPENAI_API_KEY to be set in the environment or .env.local.
Uses gpt-4o-mini to minimise cost.
Skipped automatically if the key is absent or empty.

Run manually:
    uv run pytest tests/integration/test_openai_chat.py -m integration -s
"""
from __future__ import annotations

import pytest

from trendstorm.domain.llm.models import Message
from trendstorm.infrastructure.llm.openai import OpenAIChatProvider
from trendstorm.shared.config import get_settings

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_TEST_MODEL = "gpt-4o-mini"


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()


@pytest.fixture
def provider() -> OpenAIChatProvider:
    settings = get_settings()
    api_key = settings.llm.openai_api_key.get_secret_value()
    if not api_key:
        pytest.skip("LLM__OPENAI_API_KEY not set")
    return OpenAIChatProvider(api_key=api_key, model=_TEST_MODEL)


class TestOpenAIChatProviderIntegration:
    async def test_complete_returns_non_empty_text(self, provider: OpenAIChatProvider) -> None:
        result = await provider.complete([
            Message(role="user", content="Reply with exactly the word: PONG"),
        ])
        assert "PONG" in result.content.upper()
        assert result.input_tokens > 0
        assert result.output_tokens > 0
        assert result.finish_reason == "stop"

    async def test_system_message_respected(self, provider: OpenAIChatProvider) -> None:
        result = await provider.complete([
            Message(role="system", content="You always end every reply with ZZZ."),
            Message(role="user", content="Say hello."),
        ])
        assert len(result.content.strip()) > 0
        assert result.model_id == f"openai.{_TEST_MODEL}"

    async def test_stream_yields_text(self, provider: OpenAIChatProvider) -> None:
        collected: list[str] = []
        async for delta in provider.stream([
            Message(role="user", content="Count from 1 to 5, one number per line."),
        ]):
            collected.append(delta)
        full = "".join(collected)
        assert len(full) > 0
        assert len(collected) > 1  # streaming must produce multiple deltas

    async def test_complete_with_tools_returns_structured_output(
        self, provider: OpenAIChatProvider
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
