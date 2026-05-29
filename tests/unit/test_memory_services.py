"""Unit tests for memory services — supersede detection and retrieval.

No I/O: ChromaDB and Mongo are replaced with async mocks.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trendstorm.domain.memories.models import Memory, MemoryKind, MemorySource
from trendstorm.services.memory.retrieval import MemoryRetriever, RetrievedMemory


def _now() -> datetime:
    return datetime.now(UTC)


def _memory(
    *,
    memory_id: str = "mem-1",
    tenant_id: str = "tenant-a",
    category_id: str = "cat-1",
    kind: MemoryKind = MemoryKind.SEMANTIC,
    content: str = "GPT-5 launched in Q2 2025.",
    is_active: bool = True,
) -> Memory:
    return Memory(
        id=memory_id,
        tenant_id=tenant_id,
        category_id=category_id,
        kind=kind,
        source=MemorySource.EXTRACTED,
        content=content,
        confidence=0.9,
        source_job_id="job-1",
        source_analysis_id="analysis-1",
        is_active=is_active,
        created_at=_now(),
        updated_at=_now(),
    )


# ---------------------------------------------------------------------------
# Helpers for fake ChromaDB hits
# ---------------------------------------------------------------------------

class FakeChromaHit:
    def __init__(self, id: str, score: float) -> None:
        self.id = id
        self.score = score


@pytest.mark.unit
class TestSupersedeLowSimilarityNoSupersede:
    """Below-threshold similarity → no supersede."""

    @pytest.mark.asyncio
    async def test_no_supersede_when_similarity_below_threshold(self) -> None:
        from trendstorm.services.memory.semantic_extractor import SemanticMemoryExtractor

        mock_chat = MagicMock()
        mock_embed = MagicMock()
        mock_embed.model_id = "text-embedding-004"
        mock_embed.embed_batch = AsyncMock(return_value=[[0.1, 0.2, 0.3]])

        mock_repo = MagicMock()
        mock_repo.supersede = AsyncMock()

        # ChromaDB returns one hit but score is below threshold (0.92)
        mock_store = MagicMock()
        mock_store.query_memories = AsyncMock(
            return_value=[FakeChromaHit(id="old-mem", score=0.85)]
        )

        extractor = SemanticMemoryExtractor(
            chat_provider=mock_chat,
            embed=mock_embed,
            vector_store=mock_store,
            memory_repo=mock_repo,
            supersede_threshold=0.92,
        )
        await extractor._maybe_supersede(
            embedding=[0.1, 0.2, 0.3],
            collection="col",
            new_memory_id="new-mem",
            tenant_id="tenant-a",
            category_id="cat-1",
        )
        # score 0.85 < threshold 0.92 → no supersede
        mock_repo.supersede.assert_not_called()


@pytest.mark.unit
class TestSupersedHighSimilarityDoesSuperside:
    """Above-threshold similarity → old memory is superseded."""

    @pytest.mark.asyncio
    async def test_supersede_when_similarity_above_threshold(self) -> None:
        from trendstorm.services.memory.semantic_extractor import SemanticMemoryExtractor

        mock_chat = MagicMock()
        mock_embed = MagicMock()
        mock_embed.model_id = "text-embedding-004"
        mock_embed.embed_batch = AsyncMock(return_value=[[0.1, 0.2, 0.3]])

        mock_repo = MagicMock()
        mock_repo.supersede = AsyncMock()

        mock_store = MagicMock()
        mock_store.query_memories = AsyncMock(
            return_value=[FakeChromaHit(id="old-mem", score=0.97)]
        )

        extractor = SemanticMemoryExtractor(
            chat_provider=mock_chat,
            embed=mock_embed,
            vector_store=mock_store,
            memory_repo=mock_repo,
            supersede_threshold=0.92,
        )
        await extractor._maybe_supersede(
            embedding=[0.1, 0.2, 0.3],
            collection="col",
            new_memory_id="new-mem",
            tenant_id="tenant-a",
            category_id="cat-1",
        )
        mock_repo.supersede.assert_awaited_once_with(
            "tenant-a", "old-mem", "new-mem"
        )

    @pytest.mark.asyncio
    async def test_no_self_supersede(self) -> None:
        """A memory should never supersede itself."""
        from trendstorm.services.memory.semantic_extractor import SemanticMemoryExtractor

        mock_chat = MagicMock()
        mock_embed = MagicMock()
        mock_embed.model_id = "text-embedding-004"
        mock_embed.embed_batch = AsyncMock(return_value=[[0.1, 0.2]])

        mock_repo = MagicMock()
        mock_repo.supersede = AsyncMock()

        mock_store = MagicMock()
        # Same ID as the new memory → self-supersede guard
        mock_store.query_memories = AsyncMock(
            return_value=[FakeChromaHit(id="same-id", score=1.0)]
        )

        extractor = SemanticMemoryExtractor(
            chat_provider=mock_chat,
            embed=mock_embed,
            vector_store=mock_store,
            memory_repo=mock_repo,
            supersede_threshold=0.92,
        )
        await extractor._maybe_supersede(
            embedding=[0.1, 0.2],
            collection="col",
            new_memory_id="same-id",
            tenant_id="t",
            category_id="c",
        )
        mock_repo.supersede.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_hit_no_supersede(self) -> None:
        """Empty ChromaDB result → no supersede attempted."""
        from trendstorm.services.memory.semantic_extractor import SemanticMemoryExtractor

        mock_chat = MagicMock()
        mock_embed = MagicMock()
        mock_embed.model_id = "text-embedding-004"
        mock_embed.embed_batch = AsyncMock(return_value=[[0.1]])

        mock_repo = MagicMock()
        mock_repo.supersede = AsyncMock()

        mock_store = MagicMock()
        mock_store.query_memories = AsyncMock(return_value=[])

        extractor = SemanticMemoryExtractor(
            chat_provider=mock_chat,
            embed=mock_embed,
            vector_store=mock_store,
            memory_repo=mock_repo,
            supersede_threshold=0.92,
        )
        await extractor._maybe_supersede(
            embedding=[0.1],
            collection="col",
            new_memory_id="new-mem",
            tenant_id="t",
            category_id="c",
        )
        mock_repo.supersede.assert_not_called()


@pytest.mark.unit
class TestMemoryRetriever:
    """MemoryRetriever: embedding + Chroma query + Mongo hydration."""

    @pytest.mark.asyncio
    async def test_returns_hydrated_results(self) -> None:
        mem = _memory()
        mock_embed = MagicMock()
        mock_embed.model_id = "text-embedding-004"
        mock_embed.embed_batch = AsyncMock(return_value=[[0.5, 0.6]])

        mock_store = MagicMock()
        mock_store.query_memories = AsyncMock(
            return_value=[FakeChromaHit(id=mem.id, score=0.88)]
        )

        mock_repo = MagicMock()
        mock_repo.get = AsyncMock(return_value=mem)

        retriever = MemoryRetriever(
            embed=mock_embed,
            vector_store=mock_store,
            memory_repo=mock_repo,
        )
        results = await retriever.retrieve_relevant(
            "What is the state of LLMs?",
            "tenant-a",
            "cat-1",
            top_k=5,
        )
        assert len(results) == 1
        assert isinstance(results[0], RetrievedMemory)
        assert results[0].content == mem.content
        assert results[0].score == 0.88
        assert results[0].kind == MemoryKind.SEMANTIC

    @pytest.mark.asyncio
    async def test_skips_inactive_memories(self) -> None:
        mem = _memory(is_active=False)
        mock_embed = MagicMock()
        mock_embed.model_id = "text-embedding-004"
        mock_embed.embed_batch = AsyncMock(return_value=[[0.5]])

        mock_store = MagicMock()
        mock_store.query_memories = AsyncMock(
            return_value=[FakeChromaHit(id=mem.id, score=0.95)]
        )

        mock_repo = MagicMock()
        mock_repo.get = AsyncMock(return_value=mem)

        retriever = MemoryRetriever(
            embed=mock_embed,
            vector_store=mock_store,
            memory_repo=mock_repo,
        )
        results = await retriever.retrieve_relevant(
            "query", "tenant-a", "cat-1", top_k=5
        )
        assert results == []

    @pytest.mark.asyncio
    async def test_skips_missing_mongo_docs(self) -> None:
        mock_embed = MagicMock()
        mock_embed.model_id = "text-embedding-004"
        mock_embed.embed_batch = AsyncMock(return_value=[[0.5]])

        mock_store = MagicMock()
        mock_store.query_memories = AsyncMock(
            return_value=[FakeChromaHit(id="ghost-id", score=0.9)]
        )

        mock_repo = MagicMock()
        mock_repo.get = AsyncMock(return_value=None)  # Mongo has no record

        retriever = MemoryRetriever(
            embed=mock_embed,
            vector_store=mock_store,
            memory_repo=mock_repo,
        )
        results = await retriever.retrieve_relevant(
            "query", "tenant-a", "cat-1", top_k=5
        )
        assert results == []

    @pytest.mark.asyncio
    async def test_cross_tenant_isolation(self) -> None:
        """Results are only from the queried tenant (enforced by Chroma filter)."""
        mem_a = _memory(tenant_id="tenant-a", memory_id="mem-a")
        mock_embed = MagicMock()
        mock_embed.model_id = "text-embedding-004"
        mock_embed.embed_batch = AsyncMock(return_value=[[0.5]])

        mock_store = MagicMock()
        # Chroma returns only tenant-a's memory (filter is applied inside query_memories)
        mock_store.query_memories = AsyncMock(
            return_value=[FakeChromaHit(id="mem-a", score=0.9)]
        )

        mock_repo = MagicMock()
        mock_repo.get = AsyncMock(return_value=mem_a)

        retriever = MemoryRetriever(
            embed=mock_embed,
            vector_store=mock_store,
            memory_repo=mock_repo,
        )
        results = await retriever.retrieve_relevant(
            "query", "tenant-a", "cat-1", top_k=5
        )
        # Assert Chroma was called with tenant-a's tenant_id (isolation enforced at call site)
        mock_store.query_memories.assert_awaited_once()
        call_kwargs = mock_store.query_memories.call_args.kwargs
        assert call_kwargs["tenant_id"] == "tenant-a"
        assert results[0].memory_id == "mem-a"
