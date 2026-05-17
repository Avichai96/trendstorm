"""Unit tests for OpenAIChatProvider.

All OpenAI API calls are faked via injected _client.
No real API keys or network calls are made.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from trendstorm.domain.llm.errors import LLMSchemaError, LLMTransientError
from trendstorm.domain.llm.models import Completion, Message
from trendstorm.domain.llm.providers import ChatProvider
from trendstorm.infrastructure.llm.openai import OpenAIChatProvider

# ---------------------------------------------------------------------------
# Fake OpenAI SDK objects
# ---------------------------------------------------------------------------

def _fake_tool_call(name: str, arguments: str) -> Any:
    fn = MagicMock()
    fn.name = name
    fn.arguments = arguments
    tc = MagicMock()
    tc.function = fn
    return tc


def _fake_message(
    content: str | None = "Hello",
    tool_calls: list[Any] | None = None,
) -> Any:
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls
    return msg


def _fake_choice(
    message: Any | None = None,
    finish_reason: str = "stop",
) -> Any:
    c = MagicMock()
    c.message = message or _fake_message()
    c.finish_reason = finish_reason
    return c


def _fake_usage(prompt: int = 10, completion: int = 20) -> Any:
    u = MagicMock()
    u.prompt_tokens = prompt
    u.completion_tokens = completion
    return u


def _fake_response(
    content: str = "Hello from GPT",
    finish_reason: str = "stop",
    input_tokens: int = 10,
    output_tokens: int = 20,
    tool_calls: list[Any] | None = None,
) -> Any:
    msg = _fake_message(content=content, tool_calls=tool_calls)
    r = MagicMock()
    r.choices = [_fake_choice(message=msg, finish_reason=finish_reason)]
    r.usage = _fake_usage(input_tokens, output_tokens)
    return r


def _fake_stream_chunks(texts: list[str]) -> list[Any]:
    chunks = []
    for t in texts:
        delta = MagicMock()
        delta.content = t
        ch = MagicMock()
        ch.choices = [MagicMock(delta=delta)]
        chunks.append(ch)
    return chunks


def _fake_client(
    response: Any | None = None,
    stream_texts: list[str] | None = None,
) -> Any:
    client = MagicMock()

    if stream_texts is not None:
        # When stream=True, create returns an async iterator
        async def _aiter():
            for chunk in _fake_stream_chunks(stream_texts):
                yield chunk

        async def _create(**kwargs):
            if kwargs.get("stream"):
                return _aiter()
            return response or _fake_response()

        client.chat.completions.create = AsyncMock(side_effect=_create)
    else:
        client.chat.completions.create = AsyncMock(return_value=response or _fake_response())

    return client


def _make_provider(
    model: str = "gpt-4o-mini",
    *,
    temperature: float | None = None,
    response: Any = None,
    stream_texts: list[str] | None = None,
) -> OpenAIChatProvider:
    return OpenAIChatProvider(
        api_key="fake",
        model=model,
        temperature=temperature,
        _client=_fake_client(response, stream_texts=stream_texts),
    )


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestOpenAIChatProviderProperties:
    def test_model_id_format(self) -> None:
        assert _make_provider().model_id == "openai.gpt-4o-mini"

    def test_custom_model(self) -> None:
        p = OpenAIChatProvider(api_key="x", model="gpt-4o", _client=MagicMock())
        assert p.model_id == "openai.gpt-4o"

    def test_satisfies_chat_provider_protocol(self) -> None:
        assert isinstance(_make_provider(), ChatProvider)

    def test_client_injection_stored(self) -> None:
        fake = MagicMock()
        p = OpenAIChatProvider(api_key="x", _client=fake)
        assert p._client is fake


# ---------------------------------------------------------------------------
# complete()
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestOpenAIChatComplete:
    async def test_returns_completion(self) -> None:
        p = _make_provider(response=_fake_response("Hi GPT!", input_tokens=5, output_tokens=3))
        result = await p.complete([Message(role="user", content="Hello")])
        assert isinstance(result, Completion)
        assert result.content == "Hi GPT!"
        assert result.input_tokens == 5
        assert result.output_tokens == 3
        assert result.model_id == "openai.gpt-4o-mini"

    async def test_finish_reason_stop_mapped(self) -> None:
        p = _make_provider(response=_fake_response(finish_reason="stop"))
        result = await p.complete([Message(role="user", content="Hi")])
        assert result.finish_reason == "stop"

    async def test_finish_reason_length_mapped(self) -> None:
        p = _make_provider(response=_fake_response(finish_reason="length"))
        result = await p.complete([Message(role="user", content="Hi")])
        assert result.finish_reason == "length"

    async def test_finish_reason_content_filter_mapped(self) -> None:
        p = _make_provider(response=_fake_response(finish_reason="content_filter"))
        result = await p.complete([Message(role="user", content="Hi")])
        assert result.finish_reason == "content_filter"

    async def test_system_message_stays_in_messages_array(self) -> None:
        """OpenAI keeps system messages in the messages list (unlike Anthropic/Gemini)."""
        p = _make_provider()
        msgs = [
            Message(role="system", content="You are helpful."),
            Message(role="user", content="Hello"),
        ]
        await p.complete(msgs)
        call_kwargs = p._client.chat.completions.create.call_args.kwargs
        roles = [m["role"] for m in call_kwargs["messages"]]
        assert roles == ["system", "user"]

    async def test_temperature_passed_when_set(self) -> None:
        p = _make_provider(temperature=0.5)
        await p.complete([Message(role="user", content="Hi")])
        assert p._client.chat.completions.create.call_args.kwargs["temperature"] == 0.5

    async def test_no_temperature_when_none(self) -> None:
        p = _make_provider(temperature=None)
        await p.complete([Message(role="user", content="Hi")])
        assert "temperature" not in p._client.chat.completions.create.call_args.kwargs

    async def test_content_none_becomes_empty_string(self) -> None:
        """OpenAI returns content=None for tool-only responses; we coerce to ''."""
        response = _fake_response(content=None)
        p = _make_provider(response=response)
        result = await p.complete([Message(role="user", content="Hi")])
        assert result.content == ""

    async def test_unknown_error_mapped_to_transient(self) -> None:
        client = MagicMock()
        client.chat.completions.create = AsyncMock(side_effect=RuntimeError("network err"))
        p = OpenAIChatProvider(api_key="x", _client=client)
        with pytest.raises(LLMTransientError):
            await p.complete([Message(role="user", content="Hi")])


# ---------------------------------------------------------------------------
# stream()
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestOpenAIChatStream:
    async def test_yields_text_deltas(self) -> None:
        p = _make_provider(stream_texts=["Hello", " ", "world"])
        collected = [t async for t in p.stream([Message(role="user", content="Hi")])]
        assert collected == ["Hello", " ", "world"]

    async def test_empty_stream(self) -> None:
        p = _make_provider(stream_texts=[])
        collected = [t async for t in p.stream([Message(role="user", content="Hi")])]
        assert collected == []

    async def test_none_delta_content_skipped(self) -> None:
        """Some OpenAI chunks have delta.content=None (e.g. tool call deltas)."""
        chunks = []
        for content in ["Hello", None, " world"]:
            delta = MagicMock()
            delta.content = content
            ch = MagicMock()
            ch.choices = [MagicMock(delta=delta)]
            chunks.append(ch)

        async def _aiter():
            for ch in chunks:
                yield ch

        client = MagicMock()
        client.chat.completions.create = AsyncMock(return_value=_aiter())
        p = OpenAIChatProvider(api_key="x", _client=client)
        collected = [t async for t in p.stream([Message(role="user", content="Hi")])]
        assert collected == ["Hello", " world"]


# ---------------------------------------------------------------------------
# complete_with_tools()
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestOpenAIChatCompleteWithTools:
    async def test_returns_tool_name_and_input(self) -> None:
        tool_input = {"field": "value", "number": 42}
        response = _fake_response(
            content=None,
            tool_calls=[_fake_tool_call("my_tool", json.dumps(tool_input))],
            finish_reason="tool_calls",
        )
        p = _make_provider(response=response)
        name, inp, usage = await p.complete_with_tools(
            [Message(role="user", content="analyse")],
            tools=[{"name": "my_tool", "description": "d", "input_schema": {"type": "object"}}],
            tool_choice="my_tool",
        )
        assert name == "my_tool"
        assert inp == tool_input
        assert usage.input_tokens >= 0
        assert usage.output_tokens >= 0

    async def test_tools_converted_to_openai_format(self) -> None:
        response = _fake_response(
            content=None,
            tool_calls=[_fake_tool_call("t", "{}")],
            finish_reason="tool_calls",
        )
        p = _make_provider(response=response)
        await p.complete_with_tools(
            [Message(role="user", content="go")],
            tools=[{"name": "t", "description": "desc", "input_schema": {"type": "object"}}],
            tool_choice="t",
        )
        call_kwargs = p._client.chat.completions.create.call_args.kwargs
        tools = call_kwargs["tools"]
        assert tools[0]["type"] == "function"
        assert tools[0]["function"]["name"] == "t"
        assert tools[0]["function"]["description"] == "desc"
        assert tools[0]["function"]["parameters"] == {"type": "object"}

    async def test_tool_choice_passed_as_function_choice(self) -> None:
        response = _fake_response(
            content=None,
            tool_calls=[_fake_tool_call("analysis", "{}")],
            finish_reason="tool_calls",
        )
        p = _make_provider(response=response)
        await p.complete_with_tools(
            [Message(role="user", content="go")],
            tools=[{"name": "analysis", "description": "d", "input_schema": {}}],
            tool_choice="analysis",
        )
        kwargs = p._client.chat.completions.create.call_args.kwargs
        assert kwargs["tool_choice"] == {"type": "function", "function": {"name": "analysis"}}

    async def test_no_tool_choice_when_none(self) -> None:
        response = _fake_response(
            content=None,
            tool_calls=[_fake_tool_call("t", "{}")],
            finish_reason="tool_calls",
        )
        p = _make_provider(response=response)
        await p.complete_with_tools(
            [Message(role="user", content="go")],
            tools=[{"name": "t", "description": "d", "input_schema": {}}],
        )
        kwargs = p._client.chat.completions.create.call_args.kwargs
        assert "tool_choice" not in kwargs

    async def test_raises_schema_error_when_no_tool_calls(self) -> None:
        response = _fake_response(content="prose response", finish_reason="stop")
        # message.tool_calls is None
        p = _make_provider(response=response)
        with pytest.raises(LLMSchemaError):
            await p.complete_with_tools(
                [Message(role="user", content="go")],
                tools=[{"name": "t", "description": "d", "input_schema": {}}],
            )

    async def test_raises_schema_error_on_invalid_json_arguments(self) -> None:
        response = _fake_response(
            content=None,
            tool_calls=[_fake_tool_call("t", "not-valid-json{{{{")],
            finish_reason="tool_calls",
        )
        p = _make_provider(response=response)
        with pytest.raises(LLMSchemaError, match="not valid JSON"):
            await p.complete_with_tools(
                [Message(role="user", content="go")],
                tools=[{"name": "t", "description": "d", "input_schema": {}}],
            )


# ---------------------------------------------------------------------------
# Registry — OpenAI as chat provider
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestBuildChatProviderOpenAI:
    def test_openai_chat_provider_selectable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM__DEFAULT_CHAT_PROVIDER", "openai")
        monkeypatch.setenv("LLM__OPENAI_API_KEY", "sk-fake")

        from trendstorm.shared.config import Settings, get_settings
        get_settings.cache_clear()
        settings = Settings()

        from trendstorm.infrastructure.llm.registry import build_chat_provider
        provider = build_chat_provider(settings)
        assert isinstance(provider, OpenAIChatProvider)
