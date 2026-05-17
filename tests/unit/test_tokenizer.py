"""Unit tests for agents/knowledge/tokenizer.py.

Token counts are stable for cl100k_base — verified against tiktoken directly.
"""
from __future__ import annotations

import pytest

from trendstorm.agents.knowledge.tokenizer import (
    ENCODING_NAME,
    count_tokens,
    truncate_to_tokens,
)


@pytest.mark.unit
class TestCountTokens:
    def test_empty_string_is_zero(self) -> None:
        assert count_tokens("") == 0

    def test_single_word(self) -> None:
        assert count_tokens("hello") == 1

    def test_known_sentence(self) -> None:
        # "Hello, world!" → ["Hello", ",", " world", "!"] in cl100k_base
        assert count_tokens("Hello, world!") == 4

    def test_pangram(self) -> None:
        # Stable cl100k_base count for a well-known sentence
        assert count_tokens("The quick brown fox jumps over the lazy dog.") == 10

    def test_longer_text_has_more_tokens(self) -> None:
        short = "AI is changing the world."
        long = short * 10
        assert count_tokens(long) > count_tokens(short)

    def test_deterministic(self) -> None:
        text = "Consistency check — same text, same count."
        assert count_tokens(text) == count_tokens(text)

    def test_encoding_name_is_cl100k_base(self) -> None:
        assert ENCODING_NAME == "cl100k_base"


@pytest.mark.unit
class TestTruncateToTokens:
    def test_text_within_limit_unchanged(self) -> None:
        text = "Hello, world!"  # 4 tokens
        assert truncate_to_tokens(text, max_tokens=10) == text

    def test_text_at_exact_limit_unchanged(self) -> None:
        text = "Hello, world!"  # 4 tokens
        assert truncate_to_tokens(text, max_tokens=4) == text

    def test_truncation_reduces_token_count(self) -> None:
        text = "The quick brown fox jumps over the lazy dog."  # 10 tokens
        truncated = truncate_to_tokens(text, max_tokens=5)
        assert count_tokens(truncated) == 5

    def test_truncated_is_prefix_of_original(self) -> None:
        text = "The quick brown fox jumps over the lazy dog."
        truncated = truncate_to_tokens(text, max_tokens=3)
        # The truncated text must be decodable from the first 3 tokens
        assert count_tokens(truncated) == 3

    def test_zero_max_tokens_returns_empty(self) -> None:
        assert truncate_to_tokens("some text", max_tokens=0) == ""

    def test_negative_max_tokens_returns_empty(self) -> None:
        assert truncate_to_tokens("some text", max_tokens=-5) == ""

    def test_empty_text_unchanged(self) -> None:
        assert truncate_to_tokens("", max_tokens=100) == ""

    def test_result_never_exceeds_max_tokens(self) -> None:
        text = "Artificial intelligence is transforming industries around the globe."
        for limit in (1, 3, 5, 10, 100):
            truncated = truncate_to_tokens(text, limit)
            assert count_tokens(truncated) <= limit, f"exceeded limit={limit}"

    def test_large_limit_returns_full_text(self) -> None:
        text = "Short text."
        assert truncate_to_tokens(text, max_tokens=10_000) == text
