"""Unit tests for the Category model."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from trendstorm.domain.categories.models import Category
from trendstorm.shared.ids import new_id


def _kwargs() -> dict[str, str]:
    return {"tenant_id": new_id(), "name": "AI safety"}


@pytest.mark.unit
class TestCategoryModel:
    def test_minimal_create(self) -> None:
        c = Category(**_kwargs())
        assert c.name == "AI safety"
        assert c.archived is False
        assert c.keywords == []

    def test_name_trimmed(self) -> None:
        c = Category(tenant_id=new_id(), name="  AI safety  ")
        assert c.name == "AI safety"

    def test_name_required(self) -> None:
        with pytest.raises(ValidationError):
            Category(tenant_id=new_id(), name="")

    def test_keywords_dedup_case_insensitive(self) -> None:
        c = Category(
            tenant_id=new_id(),
            name="Crypto",
            keywords=["bitcoin", "Bitcoin", "ethereum", "BITCOIN", "eth"],
        )
        # First-occurrence casing is preserved.
        assert c.keywords == ["bitcoin", "ethereum", "eth"]

    def test_keywords_strip_empty(self) -> None:
        c = Category(
            tenant_id=new_id(),
            name="X",
            keywords=["one", "", "  ", "two"],
        )
        assert c.keywords == ["one", "two"]

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            Category(tenant_id=new_id(), name="x", junk_field="not allowed")

    def test_id_is_ulid(self) -> None:
        c = Category(**_kwargs())
        assert len(c.id) == 26
