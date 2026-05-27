"""Integration tests for the categories resource.

Requires a live TrendStorm API. Mark: @staging for CI.
"""
from __future__ import annotations

import uuid

import pytest

from trendstorm_sdk import TrendStormClient


@pytest.mark.integration
class TestCategoryIntegration:
    async def test_create_list_update_roundtrip(self, ts: TrendStormClient) -> None:
        name = f"SDK Test {uuid.uuid4().hex[:8]}"
        cat = await ts.categories.create(
            name=name,
            description="Created by SDK integration test",
            keywords=["sdk", "test"],
        )
        assert cat.id
        assert cat.name == name
        assert "sdk" in cat.keywords
        assert not cat.archived

        cats = await ts.categories.list()
        ids = [c.id for c in cats.categories]
        assert cat.id in ids

        updated = await ts.categories.update(cat.id, archived=True)
        assert updated.archived

        cats_no_archived = await ts.categories.list(include_archived=False)
        assert cat.id not in [c.id for c in cats_no_archived.categories]

        cats_with_archived = await ts.categories.list(include_archived=True)
        assert cat.id in [c.id for c in cats_with_archived.categories]

    async def test_get_nonexistent_raises_not_found(self, ts: TrendStormClient) -> None:
        from trendstorm_sdk import NotFound

        fake_id = "01" + "0" * 24
        with pytest.raises(NotFound):
            await ts.categories.get(fake_id)

    async def test_cursor_pagination(self, ts: TrendStormClient) -> None:
        names = [f"SDK Page Test {uuid.uuid4().hex[:8]}" for _ in range(3)]
        created_ids = []
        for name in names:
            c = await ts.categories.create(name=name)
            created_ids.append(c.id)

        page1 = await ts.categories.list(limit=2, include_archived=True)
        assert len(page1.categories) <= 2

        if page1.next_cursor:
            page2 = await ts.categories.list(limit=2, cursor=page1.next_cursor, include_archived=True)
            p1_ids = {c.id for c in page1.categories}
            p2_ids = {c.id for c in page2.categories}
            assert p1_ids.isdisjoint(p2_ids)
