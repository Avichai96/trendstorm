"""Memory retrieval service — fetches relevant memories for an Analyst pass."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field

from trendstorm.domain.memories.models import MemoryKind
from trendstorm.infrastructure.vectors.chroma_memory_store import (
    ChromaMemoryStore,
    memory_collection_name,
)
from trendstorm.shared.logging import get_logger
from trendstorm.shared.metrics.registry import METRICS
from trendstorm.shared.tracing.semantics import Attr

if TYPE_CHECKING:
    from trendstorm.domain.llm.providers import EmbeddingProvider
    from trendstorm.domain.memories.repository import MemoryRepository

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)


class RetrievedMemory(BaseModel):
    """One relevant memory returned by MemoryRetriever."""

    model_config = ConfigDict(extra="forbid")

    memory_id: str
    content: str
    confidence: float = Field(ge=0.0, le=1.0)
    kind: MemoryKind
    score: float = Field(description="Vector similarity score [0, 1].")
    source_job_id: str
    tags: list[str] = Field(default_factory=list)


class MemoryRetriever:
    """Embeds a query and fetches the most relevant memories from ChromaDB.

    Results are Mongo-hydrated so callers receive full Memory content.
    """

    def __init__(
        self,
        embed: EmbeddingProvider,
        vector_store: ChromaMemoryStore,
        memory_repo: MemoryRepository,
    ) -> None:
        self._embed = embed
        self._vector_store = vector_store
        self._memory_repo = memory_repo

    async def retrieve_relevant(
        self,
        query: str,
        tenant_id: str,
        category_id: str,
        *,
        top_k: int = 5,
        kind: MemoryKind | None = None,
    ) -> list[RetrievedMemory]:
        """Return the top-k most relevant memories for `query`.

        Runs embedding + Chroma query + Mongo hydration. If ChromaDB returns
        an ID that no longer exists in Mongo (e.g., due to reaping), it is
        silently skipped — never crash the Analyst pass on a memory miss.
        """
        with tracer.start_as_current_span(
            "memory.retrieve",
            attributes={
                Attr.TENANT_ID: tenant_id,
                Attr.CATEGORY_ID: category_id,
                Attr.QUERY: query[:200],
            },
        ) as span:
            embedding = (await self._embed.embed_batch([query], task_type="query"))[0]
            collection = memory_collection_name(tenant_id, self._embed.model_id)

            hits = await self._vector_store.query_memories(
                collection=collection,
                query_embedding=embedding,
                n_results=top_k,
                tenant_id=tenant_id,
                category_id=category_id,
                kind=kind.value if kind else None,
            )

            # Hydrate from Mongo — fetch all concurrently.
            async def _hydrate(hit_id: str, score: float) -> RetrievedMemory | None:
                mem = await self._memory_repo.get(tenant_id, hit_id)
                if mem is None or not mem.is_active:
                    return None
                return RetrievedMemory(
                    memory_id=mem.id,
                    content=mem.content,
                    confidence=mem.confidence,
                    kind=mem.kind,
                    score=score,
                    source_job_id=mem.source_job_id,
                    tags=mem.tags,
                )

            results_raw = await asyncio.gather(
                *[_hydrate(h.id, h.score) for h in hits],
                return_exceptions=True,
            )
            results: list[RetrievedMemory] = [
                r for r in results_raw if isinstance(r, RetrievedMemory)
            ]

            span.set_attribute(Attr.MEMORY_HITS, len(results))
            span.set_attribute(Attr.MEMORY_COLLECTION, collection)
            logger.debug(
                "memory.retrieve.done",
                tenant_id=tenant_id,
                category_id=category_id,
                n_hits=len(results),
                kind=kind.value if kind else "all",
            )

            # Metrics — record for each kind present in results.
            for mk in {r.kind for r in results}:
                try:
                    METRICS.memory_retrieval_hits.labels(
                        tenant_id=tenant_id, kind=mk.value
                    ).observe(sum(1 for r in results if r.kind == mk))
                except Exception:
                    pass

            return results
