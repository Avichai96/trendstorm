"""Unit tests for domain/vectors/ models and Protocol."""
from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from trendstorm.domain.vectors.models import VectorHit
from trendstorm.domain.vectors.store import VectorStore


@pytest.mark.unit
class TestVectorHit:
    def test_valid_hit(self) -> None:
        hit = VectorHit(id="chunk-abc", score=0.87, metadata={"category_id": "cat-1"})
        assert hit.id == "chunk-abc"
        assert hit.score == pytest.approx(0.87)
        assert hit.document is None

    def test_score_boundaries(self) -> None:
        VectorHit(id="x", score=0.0)
        VectorHit(id="x", score=1.0)

    def test_score_below_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            VectorHit(id="x", score=-0.01)

    def test_score_above_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            VectorHit(id="x", score=1.01)

    def test_document_field(self) -> None:
        hit = VectorHit(id="x", score=0.5, document="some chunk text")
        assert hit.document == "some chunk text"

    def test_empty_metadata_default(self) -> None:
        hit = VectorHit(id="x", score=0.5)
        assert hit.metadata == {}

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            VectorHit(id="x", score=0.5, unknown_field="oops")  # type: ignore[call-arg]


@pytest.mark.unit
class TestVectorStoreProtocol:
    def _make_valid_store(self) -> object:
        class FakeStore:
            async def health_check(self) -> bool:
                return True

            async def upsert(
                self,
                collection: str,
                ids: list[str],
                embeddings: list[list[float]],
                documents: list[str],
                metadatas: list[dict[str, Any]],
            ) -> None:
                pass

            async def query(
                self,
                collection: str,
                query_embedding: list[float],
                n_results: int,
                where: dict[str, Any] | None = None,
            ) -> list[VectorHit]:
                return []

            async def delete_by_filter(
                self,
                collection: str,
                where: dict[str, Any],
            ) -> None:
                pass

        return FakeStore()

    def test_runtime_check_passes_for_valid_impl(self) -> None:
        assert isinstance(self._make_valid_store(), VectorStore)

    def test_runtime_check_fails_missing_upsert(self) -> None:
        class NoUpsert:
            async def health_check(self) -> bool:
                return True

            async def query(
                self,
                collection: str,
                query_embedding: list[float],
                n_results: int,
                where: dict[str, Any] | None = None,
            ) -> list[VectorHit]:
                return []

            async def delete_by_filter(
                self, collection: str, where: dict[str, Any]
            ) -> None:
                pass

        assert not isinstance(NoUpsert(), VectorStore)

    def test_runtime_check_fails_missing_query(self) -> None:
        class NoQuery:
            async def health_check(self) -> bool:
                return True

            async def upsert(
                self,
                collection: str,
                ids: list[str],
                embeddings: list[list[float]],
                documents: list[str],
                metadatas: list[dict[str, Any]],
            ) -> None:
                pass

            async def delete_by_filter(
                self, collection: str, where: dict[str, Any]
            ) -> None:
                pass

        assert not isinstance(NoQuery(), VectorStore)
