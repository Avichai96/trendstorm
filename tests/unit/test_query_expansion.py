"""Unit tests for services/retrieval/query_expansion.py.

All LLM calls are faked via injected _prompt_text and a fake ChatProvider.
No real API calls are made.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from trendstorm.domain.llm.models import Completion, Message
from trendstorm.domain.llm.providers import ChatProvider
from trendstorm.services.retrieval.query_expansion import (
    QueryExpander,
    _load_prompt,
    _parse_sub_queries,
)

# ---------------------------------------------------------------------------
# Fake ChatProvider
# ---------------------------------------------------------------------------

def _fake_provider(response_text: str) -> ChatProvider:
    class FakeChat:
        @property
        def model_id(self) -> str:
            return "fake.model"

        async def complete(self, messages: list[Message]) -> Completion:
            return Completion(
                content=response_text,
                model_id="fake.model",
                input_tokens=10,
                output_tokens=20,
                finish_reason="stop",
            )

        def stream(self, messages: list[Message]) -> AsyncIterator[str]:
            raise NotImplementedError

    return FakeChat()  # type: ignore[return-value]


def _fake_failing_provider() -> ChatProvider:
    class FailingChat:
        @property
        def model_id(self) -> str:
            return "fake.failing"

        async def complete(self, messages: list[Message]) -> Completion:
            raise RuntimeError("LLM unavailable")

        def stream(self, messages: list[Message]) -> AsyncIterator[str]:
            raise NotImplementedError

    return FailingChat()  # type: ignore[return-value]


_STUB_PROMPT = "Generate sub-queries."


# ---------------------------------------------------------------------------
# _parse_sub_queries (pure function)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestParseSubQueries:
    def test_plain_lines(self) -> None:
        raw = "query one\nquery two\nquery three"
        result = _parse_sub_queries(raw, count=3)
        assert result == ["query one", "query two", "query three"]

    def test_strips_numbered_prefixes(self) -> None:
        raw = "1. first query\n2) second query\n3. third query"
        result = _parse_sub_queries(raw, count=3)
        assert result == ["first query", "second query", "third query"]

    def test_strips_bullet_prefixes(self) -> None:
        raw = "- bullet one\n• bullet two\n* bullet three\n# hash four"
        result = _parse_sub_queries(raw, count=4)
        assert result == ["bullet one", "bullet two", "bullet three", "hash four"]

    def test_respects_count_limit(self) -> None:
        raw = "a\nb\nc\nd\ne"
        result = _parse_sub_queries(raw, count=3)
        assert result == ["a", "b", "c"]

    def test_skips_empty_lines(self) -> None:
        raw = "\nquery one\n\nquery two\n"
        result = _parse_sub_queries(raw, count=5)
        assert result == ["query one", "query two"]

    def test_deduplicates_case_insensitively(self) -> None:
        raw = "AI safety\nai safety\nAI Safety\nunique query"
        result = _parse_sub_queries(raw, count=5)
        assert result == ["AI safety", "unique query"]

    def test_empty_input_returns_empty(self) -> None:
        assert _parse_sub_queries("", count=3) == []

    def test_whitespace_only_lines_skipped(self) -> None:
        raw = "   \n  \nreal query\n   "
        result = _parse_sub_queries(raw, count=3)
        assert result == ["real query"]


# ---------------------------------------------------------------------------
# _load_prompt
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestLoadPrompt:
    def test_loads_non_empty_string(self) -> None:
        prompt = _load_prompt()
        assert isinstance(prompt, str)
        assert len(prompt) > 50  # sanity: must be a real prompt, not empty

    def test_contains_key_instruction(self) -> None:
        prompt = _load_prompt()
        # Prompt must instruct the LLM to return one sub-query per line.
        assert "line" in prompt.lower() or "per line" in prompt.lower()


# ---------------------------------------------------------------------------
# QueryExpander
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestQueryExpander:
    async def test_returns_sub_queries_from_llm(self) -> None:
        provider = _fake_provider("LLM alignment techniques\nSafety in neural networks\nRLHF methods")
        expander = QueryExpander(provider, _prompt_text=_STUB_PROMPT)
        result = await expander.expand("AI safety", count=3)
        # Original query is prepended + LLM sub-queries
        assert "AI safety" in result
        assert len(result) >= 1

    async def test_original_query_always_present(self) -> None:
        provider = _fake_provider("sub-query alpha\nsub-query beta")
        expander = QueryExpander(provider, _prompt_text=_STUB_PROMPT)
        result = await expander.expand("original query", count=3)
        assert "original query" in result

    async def test_count_is_respected(self) -> None:
        # LLM returns 5 sub-queries, we ask for 3
        provider = _fake_provider("a\nb\nc\nd\ne")
        expander = QueryExpander(provider, _prompt_text=_STUB_PROMPT)
        result = await expander.expand("query", count=3)
        assert len(result) <= 3

    async def test_fallback_on_llm_failure(self) -> None:
        expander = QueryExpander(_fake_failing_provider(), _prompt_text=_STUB_PROMPT)
        result = await expander.expand("my query", count=3)
        assert result == ["my query"]

    async def test_fallback_on_empty_response(self) -> None:
        provider = _fake_provider("")
        expander = QueryExpander(provider, _prompt_text=_STUB_PROMPT)
        result = await expander.expand("my query", count=3)
        assert result == ["my query"]

    async def test_fallback_on_whitespace_only_response(self) -> None:
        provider = _fake_provider("   \n\n   ")
        expander = QueryExpander(provider, _prompt_text=_STUB_PROMPT)
        result = await expander.expand("my query", count=3)
        assert result == ["my query"]

    async def test_provider_receives_system_and_user_messages(self) -> None:
        received: list[list[Message]] = []

        class CapturingChat:
            @property
            def model_id(self) -> str:
                return "fake.model"

            async def complete(self, messages: list[Message]) -> Completion:
                received.append(messages)
                return Completion(
                    content="sub-query one\nsub-query two",
                    model_id="fake.model",
                    input_tokens=5,
                    output_tokens=10,
                    finish_reason="stop",
                )

            def stream(self, messages: list[Message]) -> AsyncIterator[str]:
                raise NotImplementedError

        expander = QueryExpander(
            CapturingChat(),  # type: ignore[arg-type]
            _prompt_text=_STUB_PROMPT,
        )
        await expander.expand("test query", count=2)
        assert len(received) == 1
        msgs = received[0]
        roles = [m.role for m in msgs]
        assert roles == ["system", "user"]
        assert "test query" in msgs[1].content
        assert "2" in msgs[1].content   # count injected into user message

    async def test_numbered_llm_output_is_parsed(self) -> None:
        provider = _fake_provider("1. first sub-query\n2. second sub-query\n3. third sub-query")
        expander = QueryExpander(provider, _prompt_text=_STUB_PROMPT)
        result = await expander.expand("original", count=4)
        # Numbering must be stripped
        for q in result:
            assert not q[0].isdigit()

    async def test_custom_prompt_text_used(self) -> None:
        custom_prompt = "CUSTOM SYSTEM PROMPT"
        received_prompts: list[str] = []

        class CapturingChat:
            @property
            def model_id(self) -> str:
                return "fake.model"

            async def complete(self, messages: list[Message]) -> Completion:
                received_prompts.append(messages[0].content)
                return Completion(
                    content="only one result",
                    model_id="fake.model",
                    input_tokens=1,
                    output_tokens=1,
                    finish_reason="stop",
                )

            def stream(self, messages: list[Message]) -> AsyncIterator[str]:
                raise NotImplementedError

        expander = QueryExpander(
            CapturingChat(),  # type: ignore[arg-type]
            _prompt_text=custom_prompt,
        )
        await expander.expand("q", count=2)
        assert received_prompts[0] == custom_prompt
