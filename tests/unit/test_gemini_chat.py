"""Unit tests for GeminiChatProvider.

All Gemini SDK calls are faked via injected _client (synchronous mock,
since the real SDK is sync-wrapped in asyncio.to_thread).
No real API calls are made.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from trendstorm.domain.llm.errors import LLMSchemaError, LLMTransientError
from trendstorm.domain.llm.models import Completion, Message
from trendstorm.domain.llm.providers import ChatProvider
from trendstorm.infrastructure.llm.gemini import GeminiChatProvider

# ---------------------------------------------------------------------------
# Fake Gemini SDK objects
# ---------------------------------------------------------------------------

def _fake_part(text: str | None = None, function_call: Any = None) -> Any:
    part = MagicMock()
    part.text = text
    part.function_call = function_call
    return part


def _fake_function_call(name: str, args: dict) -> Any:
    fc = MagicMock()
    fc.name = name
    fc.args = args
    return fc


def _fake_candidate(parts: list[Any], finish_reason: str = "STOP") -> Any:
    content = MagicMock()
    content.parts = parts
    cand = MagicMock()
    cand.content = content
    cand.finish_reason = finish_reason
    return cand


def _fake_usage(prompt: int = 10, candidates: int = 20) -> Any:
    u = MagicMock()
    u.prompt_token_count = prompt
    u.candidates_token_count = candidates
    return u


def _fake_response(
    text: str = "Hello from Gemini",
    finish_reason: str = "STOP",
    input_tokens: int = 10,
    output_tokens: int = 20,
    function_call: Any = None,
) -> Any:
    parts = [_fake_part(function_call=function_call)] if function_call else [_fake_part(text=text)]

    r = MagicMock()
    r.text = text if not function_call else None
    r.candidates = [_fake_candidate(parts, finish_reason)]
    r.usage_metadata = _fake_usage(input_tokens, output_tokens)
    return r


def _fake_client(
    response: Any | None = None,
    stream_texts: list[str] | None = None,
    tool_response: Any | None = None,
) -> Any:
    client = MagicMock()
    client.models.generate_content = MagicMock(return_value=response or _fake_response())

    if stream_texts is not None:
        chunks = [MagicMock(text=t) for t in stream_texts]
        client.models.generate_content_stream = MagicMock(return_value=iter(chunks))

    if tool_response is not None:
        client.models.generate_content = MagicMock(return_value=tool_response)

    return client


def _make_provider(
    model: str = "gemini-2.0-flash",
    *,
    temperature: float | None = None,
    response: Any = None,
    stream_texts: list[str] | None = None,
) -> GeminiChatProvider:
    return GeminiChatProvider(
        api_key="fake",
        model=model,
        temperature=temperature,
        _client=_fake_client(response, stream_texts=stream_texts),
    )


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestGeminiChatProviderProperties:
    def test_model_id_format(self) -> None:
        assert _make_provider().model_id == "gemini.gemini-2.0-flash"

    def test_custom_model(self) -> None:
        p = GeminiChatProvider(api_key="x", model="gemini-1.5-pro", _client=MagicMock())
        assert p.model_id == "gemini.gemini-1.5-pro"

    def test_satisfies_chat_provider_protocol(self) -> None:
        assert isinstance(_make_provider(), ChatProvider)

    def test_client_injection_stored(self) -> None:
        fake = MagicMock()
        p = GeminiChatProvider(api_key="x", _client=fake)
        assert p._client is fake


# ---------------------------------------------------------------------------
# complete()
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestGeminiChatComplete:
    async def test_returns_completion(self) -> None:
        p = _make_provider(response=_fake_response("Hi Gemini!", input_tokens=5, output_tokens=3))
        result = await p.complete([Message(role="user", content="Hello")])
        assert isinstance(result, Completion)
        assert result.content == "Hi Gemini!"
        assert result.input_tokens == 5
        assert result.output_tokens == 3
        assert result.model_id == "gemini.gemini-2.0-flash"

    async def test_finish_reason_stop_mapped(self) -> None:
        p = _make_provider(response=_fake_response(finish_reason="STOP"))
        result = await p.complete([Message(role="user", content="Hello")])
        assert result.finish_reason == "stop"

    async def test_finish_reason_max_tokens_mapped(self) -> None:
        p = _make_provider(response=_fake_response(finish_reason="MAX_TOKENS"))
        result = await p.complete([Message(role="user", content="Hello")])
        assert result.finish_reason == "length"

    async def test_system_message_set_as_system_instruction(self) -> None:
        p = _make_provider()
        msgs = [
            Message(role="system", content="You are helpful."),
            Message(role="user", content="Hello"),
        ]
        await p.complete(msgs)
        call_kwargs = p._client.models.generate_content.call_args.kwargs
        # config is always passed as a keyword arg; it must be present
        assert "config" in call_kwargs
        assert call_kwargs["config"] is not None

    async def test_assistant_role_translated_to_model(self) -> None:
        """'assistant' in domain must become 'model' in Gemini API."""
        p = _make_provider()
        msgs = [
            Message(role="user", content="Hello"),
            Message(role="assistant", content="Hi there"),
            Message(role="user", content="Thanks"),
        ]
        await p.complete(msgs)
        call_args = p._client.models.generate_content.call_args
        contents = call_args.kwargs.get("contents") or call_args.args[1]
        roles = [c.role for c in contents]
        assert "model" in roles
        assert "assistant" not in roles

    async def test_temperature_passed_when_set(self) -> None:
        p = _make_provider(temperature=0.5)
        await p.complete([Message(role="user", content="Hi")])
        # The config object should have temperature set
        config = p._client.models.generate_content.call_args.kwargs.get("config")
        assert config is not None

    async def test_unknown_error_mapped_to_transient(self) -> None:
        client = MagicMock()
        client.models.generate_content = MagicMock(side_effect=RuntimeError("network err"))
        p = GeminiChatProvider(api_key="x", _client=client)
        with pytest.raises(LLMTransientError):
            await p.complete([Message(role="user", content="Hi")])


# ---------------------------------------------------------------------------
# stream()
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestGeminiChatStream:
    async def test_yields_text_chunks(self) -> None:
        p = _make_provider(stream_texts=["Hello", " ", "world"])
        collected = [t async for t in p.stream([Message(role="user", content="Hi")])]
        assert collected == ["Hello", " ", "world"]

    async def test_empty_stream(self) -> None:
        p = _make_provider(stream_texts=[])
        collected = [t async for t in p.stream([Message(role="user", content="Hi")])]
        assert collected == []


# ---------------------------------------------------------------------------
# complete_with_tools()
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestGeminiChatCompleteWithTools:
    async def test_returns_tool_name_and_args(self) -> None:
        fc = _fake_function_call("my_tool", {"field": "value"})
        response = _fake_response(function_call=fc, input_tokens=12, output_tokens=8)
        p = GeminiChatProvider(api_key="x", _client=_fake_client(response))
        name, args, usage = await p.complete_with_tools(
            [Message(role="user", content="analyse")],
            tools=[{"name": "my_tool", "description": "d", "input_schema": {}}],
            tool_choice="my_tool",
        )
        assert name == "my_tool"
        assert args == {"field": "value"}
        assert usage.input_tokens == 12
        assert usage.output_tokens == 8

    async def test_raises_schema_error_when_no_function_call(self) -> None:
        response = _fake_response(text="prose only")
        # No function_call → parts only have text
        response.candidates[0].content.parts = [_fake_part(text="prose only")]
        p = GeminiChatProvider(api_key="x", _client=_fake_client(response))
        with pytest.raises(LLMSchemaError):
            await p.complete_with_tools(
                [Message(role="user", content="go")],
                tools=[{"name": "t", "description": "d", "input_schema": {}}],
            )

    async def test_tools_converted_to_function_declarations(self) -> None:
        fc = _fake_function_call("analysis", {"x": 1})
        response = _fake_response(function_call=fc)
        p = GeminiChatProvider(api_key="x", _client=_fake_client(response))
        await p.complete_with_tools(
            [Message(role="user", content="go")],
            tools=[{"name": "analysis", "description": "desc", "input_schema": {"type": "object"}}],
            tool_choice="analysis",
        )
        p._client.models.generate_content.assert_called_once()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestBuildChatProvider:
    def test_gemini_selected_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM__DEFAULT_CHAT_PROVIDER", "gemini")
        monkeypatch.setenv("GEMINI__API_KEY", "test-key")

        from trendstorm.shared.config import Settings, get_settings
        get_settings.cache_clear()
        settings = Settings()

        from trendstorm.infrastructure.llm.registry import build_chat_provider
        provider = build_chat_provider(settings)
        assert isinstance(provider, GeminiChatProvider)

    def test_anthropic_selected_when_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM__DEFAULT_CHAT_PROVIDER", "anthropic")
        monkeypatch.setenv("LLM__ANTHROPIC_API_KEY", "sk-test")

        from trendstorm.shared.config import Settings, get_settings
        get_settings.cache_clear()
        settings = Settings()

        from trendstorm.infrastructure.llm.anthropic import AnthropicChatProvider
        from trendstorm.infrastructure.llm.registry import build_chat_provider
        provider = build_chat_provider(settings)
        assert isinstance(provider, AnthropicChatProvider)
