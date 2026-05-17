"""Unit tests for AnthropicChatProvider.

All Anthropic API calls are faked via injected _client.
No real API keys or network calls are made.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from trendstorm.domain.llm.errors import (
    LLMRateLimitError,
    LLMSchemaError,
    LLMTransientError,
)
from trendstorm.domain.llm.models import Completion, Message
from trendstorm.domain.llm.providers import ChatProvider
from trendstorm.infrastructure.llm.anthropic import AnthropicChatProvider

# ---------------------------------------------------------------------------
# Fake Anthropic SDK objects
# ---------------------------------------------------------------------------

def _text_block(text: str) -> Any:
    b = MagicMock()
    b.type = "text"
    b.text = text
    return b


def _tool_use_block(name: str, tool_input: dict) -> Any:
    b = MagicMock()
    b.type = "tool_use"
    b.name = name
    b.input = tool_input
    return b


def _fake_response(
    text: str = "Hello from Claude",
    stop_reason: str = "end_turn",
    input_tokens: int = 10,
    output_tokens: int = 20,
    cached_tokens: int = 0,
    content: list[Any] | None = None,
) -> Any:
    r = MagicMock()
    r.content = content if content is not None else [_text_block(text)]
    r.stop_reason = stop_reason
    r.usage = MagicMock(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cached_tokens,
    )
    return r


def _fake_client(response: Any = None, *, stream_texts: list[str] | None = None) -> Any:
    """Build a mock AsyncAnthropic client."""
    client = MagicMock()

    # complete()
    client.messages.create = AsyncMock(return_value=response or _fake_response())

    # stream()
    if stream_texts is not None:
        async def _text_stream():
            for t in stream_texts:
                yield t

        stream_ctx = MagicMock()
        stream_ctx.__aenter__ = AsyncMock(return_value=stream_ctx)
        stream_ctx.__aexit__ = AsyncMock(return_value=False)
        stream_ctx.text_stream = _text_stream()
        client.messages.stream = MagicMock(return_value=stream_ctx)

    return client


def _make_provider(
    model: str = "claude-sonnet-4-6",
    *,
    cache: bool = True,
    temperature: float | None = None,
    response: Any = None,
    stream_texts: list[str] | None = None,
) -> AnthropicChatProvider:
    return AnthropicChatProvider(
        api_key="fake",
        model=model,
        cache_system_prompt=cache,
        temperature=temperature,
        _client=_fake_client(response, stream_texts=stream_texts),
    )


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestAnthropicChatProviderProperties:
    def test_model_id_format(self) -> None:
        assert _make_provider().model_id == "anthropic.claude-sonnet-4-6"

    def test_custom_model(self) -> None:
        p = AnthropicChatProvider(api_key="x", model="claude-opus-4-7", _client=MagicMock())
        assert p.model_id == "anthropic.claude-opus-4-7"

    def test_satisfies_chat_provider_protocol(self) -> None:
        assert isinstance(_make_provider(), ChatProvider)

    def test_client_injection_stored(self) -> None:
        fake = MagicMock()
        p = AnthropicChatProvider(api_key="x", _client=fake)
        assert p._client is fake


# ---------------------------------------------------------------------------
# complete()
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestAnthropicComplete:
    async def test_returns_completion_object(self) -> None:
        p = _make_provider(response=_fake_response("Hello!", input_tokens=5, output_tokens=3))
        result = await p.complete([Message(role="user", content="Hi")])
        assert isinstance(result, Completion)
        assert result.content == "Hello!"
        assert result.input_tokens == 5
        assert result.output_tokens == 3
        assert result.model_id == "anthropic.claude-sonnet-4-6"

    async def test_finish_reason_end_turn_mapped_to_stop(self) -> None:
        p = _make_provider(response=_fake_response(stop_reason="end_turn"))
        result = await p.complete([Message(role="user", content="Hi")])
        assert result.finish_reason == "stop"

    async def test_finish_reason_max_tokens_mapped_to_length(self) -> None:
        p = _make_provider(response=_fake_response(stop_reason="max_tokens"))
        result = await p.complete([Message(role="user", content="Hi")])
        assert result.finish_reason == "length"

    async def test_system_message_extracted_and_passed_separately(self) -> None:
        p = _make_provider()
        msgs = [
            Message(role="system", content="You are helpful."),
            Message(role="user", content="Hello"),
        ]
        await p.complete(msgs)
        call_kwargs = p._client.messages.create.call_args.kwargs
        # System must not appear in messages list
        assert all(m["role"] != "system" for m in call_kwargs["messages"])
        assert "system" in call_kwargs

    async def test_system_message_has_cache_control_when_enabled(self) -> None:
        p = _make_provider(cache=True)
        msgs = [
            Message(role="system", content="System prompt here."),
            Message(role="user", content="Hi"),
        ]
        await p.complete(msgs)
        system = p._client.messages.create.call_args.kwargs["system"]
        assert isinstance(system, list)
        assert system[0]["cache_control"] == {"type": "ephemeral"}

    async def test_system_message_is_plain_string_when_cache_disabled(self) -> None:
        p = _make_provider(cache=False)
        msgs = [
            Message(role="system", content="System prompt."),
            Message(role="user", content="Hi"),
        ]
        await p.complete(msgs)
        system = p._client.messages.create.call_args.kwargs["system"]
        assert isinstance(system, str)
        assert "cache_control" not in str(system)

    async def test_temperature_passed_when_set(self) -> None:
        p = _make_provider(temperature=0.0)
        await p.complete([Message(role="user", content="Hi")])
        assert p._client.messages.create.call_args.kwargs["temperature"] == 0.0

    async def test_no_temperature_when_none(self) -> None:
        p = _make_provider(temperature=None)
        await p.complete([Message(role="user", content="Hi")])
        assert "temperature" not in p._client.messages.create.call_args.kwargs

    async def test_multiple_text_blocks_concatenated(self) -> None:
        response = _fake_response(
            content=[_text_block("Hello "), _text_block("world")]
        )
        p = _make_provider(response=response)
        result = await p.complete([Message(role="user", content="Hi")])
        assert result.content == "Hello world"

    async def test_rate_limit_error_propagates(self) -> None:
        client = MagicMock()
        client.messages.create = AsyncMock(side_effect=LLMRateLimitError("429"))
        p = AnthropicChatProvider(api_key="x", _client=client)
        with pytest.raises(LLMRateLimitError):
            await p.complete([Message(role="user", content="Hi")])

    async def test_transient_error_propagates(self) -> None:
        client = MagicMock()
        client.messages.create = AsyncMock(side_effect=LLMTransientError("503"))
        p = AnthropicChatProvider(api_key="x", _client=client)
        with pytest.raises(LLMTransientError):
            await p.complete([Message(role="user", content="Hi")])

    async def test_unknown_error_mapped_to_transient(self) -> None:
        client = MagicMock()
        client.messages.create = AsyncMock(side_effect=RuntimeError("connection reset"))
        p = AnthropicChatProvider(api_key="x", _client=client)
        with pytest.raises(LLMTransientError):
            await p.complete([Message(role="user", content="Hi")])


# ---------------------------------------------------------------------------
# stream()
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestAnthropicStream:
    async def test_yields_text_deltas(self) -> None:
        p = _make_provider(stream_texts=["Hello", " ", "world"])
        collected: list[str] = []
        async for delta in p.stream([Message(role="user", content="Hi")]):
            collected.append(delta)
        assert collected == ["Hello", " ", "world"]

    async def test_empty_stream(self) -> None:
        p = _make_provider(stream_texts=[])
        collected = [t async for t in p.stream([Message(role="user", content="Hi")])]
        assert collected == []

    async def test_system_message_honored_in_stream(self) -> None:
        p = _make_provider(cache=True, stream_texts=["hi"])
        msgs = [
            Message(role="system", content="Be concise."),
            Message(role="user", content="Hello"),
        ]
        _ = [t async for t in p.stream(msgs)]
        stream_kwargs = p._client.messages.stream.call_args.kwargs
        assert "system" in stream_kwargs


# ---------------------------------------------------------------------------
# complete_with_tools()
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestAnthropicCompleteWithTools:
    async def test_returns_tool_name_and_input(self) -> None:
        tool_input = {"field": "value", "number": 42}
        response = _fake_response(
            content=[_tool_use_block("my_tool", tool_input)],
            stop_reason="tool_use",
            input_tokens=15,
            output_tokens=25,
        )
        p = _make_provider(response=response)
        name, inp, usage = await p.complete_with_tools(
            [Message(role="user", content="Analyse this")],
            tools=[{"name": "my_tool", "description": "d", "input_schema": {}}],
            tool_choice="my_tool",
        )
        assert name == "my_tool"
        assert inp == tool_input
        assert usage.input_tokens == 15
        assert usage.output_tokens == 25
        assert usage.cached_tokens == 0

    async def test_tool_choice_passed_to_api(self) -> None:
        response = _fake_response(
            content=[_tool_use_block("analysis", {})], stop_reason="tool_use"
        )
        p = _make_provider(response=response)
        await p.complete_with_tools(
            [Message(role="user", content="go")],
            tools=[{"name": "analysis", "description": "d", "input_schema": {}}],
            tool_choice="analysis",
        )
        kwargs = p._client.messages.create.call_args.kwargs
        assert kwargs["tool_choice"] == {"type": "tool", "name": "analysis"}

    async def test_no_tool_choice_when_none(self) -> None:
        response = _fake_response(
            content=[_tool_use_block("t", {})], stop_reason="tool_use"
        )
        p = _make_provider(response=response)
        await p.complete_with_tools(
            [Message(role="user", content="go")],
            tools=[{"name": "t", "description": "d", "input_schema": {}}],
        )
        kwargs = p._client.messages.create.call_args.kwargs
        assert "tool_choice" not in kwargs

    async def test_raises_schema_error_when_no_tool_use_block(self) -> None:
        response = _fake_response(text="prose response", stop_reason="end_turn")
        p = _make_provider(response=response)
        with pytest.raises(LLMSchemaError):
            await p.complete_with_tools(
                [Message(role="user", content="go")],
                tools=[{"name": "t", "description": "d", "input_schema": {}}],
            )

    async def test_system_message_cached_in_tool_call(self) -> None:
        response = _fake_response(
            content=[_tool_use_block("t", {})], stop_reason="tool_use"
        )
        p = _make_provider(cache=True, response=response)
        await p.complete_with_tools(
            [
                Message(role="system", content="System prompt."),
                Message(role="user", content="go"),
            ],
            tools=[{"name": "t", "description": "d", "input_schema": {}}],
        )
        system = p._client.messages.create.call_args.kwargs["system"]
        assert isinstance(system, list)
        assert system[0]["cache_control"] == {"type": "ephemeral"}
