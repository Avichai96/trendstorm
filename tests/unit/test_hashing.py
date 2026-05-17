"""Unit tests for agents/scout/hashing.py — pure functions, no I/O."""
from __future__ import annotations

import hashlib

import pytest

from trendstorm.agents.scout.hashing import content_hash


@pytest.mark.unit
class TestContentHash:
    def test_returns_64_char_hex(self) -> None:
        h = content_hash("hello world")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_deterministic(self) -> None:
        assert content_hash("same text") == content_hash("same text")

    def test_different_inputs_differ(self) -> None:
        assert content_hash("article A") != content_hash("article B")

    def test_empty_string(self) -> None:
        h = content_hash("")
        assert len(h) == 64

    def test_matches_stdlib_sha256(self) -> None:
        text = "verify against stdlib"
        expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert content_hash(text) == expected

    def test_unicode_text(self) -> None:
        h1 = content_hash("café résumé naïve")
        h2 = content_hash("café résumé naïve")
        assert h1 == h2
        assert h1 != content_hash("cafe resume naive")

    def test_whitespace_sensitive(self) -> None:
        assert content_hash("a b") != content_hash("a  b")
