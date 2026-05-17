"""Unit tests for ULID generation."""
from __future__ import annotations

import pytest

from trendstorm.shared.ids import is_valid_id, new_id


@pytest.mark.unit
class TestIds:
    def test_new_id_length_is_26(self) -> None:
        assert len(new_id()) == 26

    def test_new_id_is_unique(self) -> None:
        ids = {new_id() for _ in range(1000)}
        assert len(ids) == 1000

    def test_is_valid_accepts_ulid(self) -> None:
        assert is_valid_id(new_id())

    @pytest.mark.parametrize("invalid", ["", "abc", "x" * 26, "not-a-ulid", "123"])
    def test_is_valid_rejects_garbage(self, invalid: str) -> None:
        assert is_valid_id(invalid) is False
