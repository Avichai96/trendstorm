"""Episodic memory writer — one record per completed job (Phase 15.5).

Episodic memory does NOT go through HITL review. It is a factual log of what
happened (which job, which analysis summary, what score), not a claim about the
world. It is safe to write unconditionally after a successful publish.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from opentelemetry import trace

from trendstorm.domain.memories.models import Memory, MemoryKind, MemorySource
from trendstorm.infrastructure.vectors.chroma_memory_store import (
    ChromaMemoryStore,
    memory_collection_name,
)
from trendstorm.shared.logging import get_logger
from trendstorm.shared.metrics.registry import METRICS, StatusLabel
from trendstorm.shared.tracing.semantics import Attr

if TYPE_CHECKING:
    from trendstorm.domain.analyses.models import Analysis
    from trendstorm.domain.llm.providers import EmbeddingProvider
    from trendstorm.domain.memories.repository import MemoryRepository

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)


class EpisodicMemoryWriter:
    """Writes one episodic Memory per completed job.

    Idempotent: if a memory already exists for `job_id`, it skips the write.
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

    async def write(
        self,
        *,
        analysis: Analysis,
        tenant_id: str,
        job_id: str,
        category_id: str,
    ) -> Memory | None:
        """Write an episodic memory for a completed job.

        Returns the persisted Memory on success, None if already written.
        """
        with tracer.start_as_current_span(
            "memory.episodic.write",
            attributes={
                Attr.TENANT_ID: tenant_id,
                Attr.JOB_ID: job_id,
                Attr.CATEGORY_ID: category_id,
            },
        ) as span:
            t0 = time.perf_counter()
            try:
                if await self._memory_repo.exists_for_job(tenant_id, job_id):
                    logger.info("memory.episodic.skip_idempotent", job_id=job_id)
                    return None

                # Build the episodic content from the analysis summary.
                # Keep it concise — it is what future analyses see as "historical context."
                content = (
                    f"Job {job_id} analysed category '{category_id}'. "
                    f"Summary: {analysis.summary[:600]}"
                )
                if analysis.validator_score:
                    content += f" (validator_score={analysis.validator_score:.2f})"

                mem = Memory(
                    tenant_id=tenant_id,
                    category_id=category_id,
                    kind=MemoryKind.EPISODIC,
                    source=MemorySource.JOB_OUTCOME,
                    content=content,
                    confidence=min(1.0, max(0.0, analysis.validator_score or 0.5)),
                    source_job_id=job_id,
                    source_analysis_id=analysis.id,
                    tags=["episodic"],
                )

                # Embed and upsert to ChromaDB.
                embeddings = await self._embed.embed_batch([content])
                embedding = embeddings.vectors[0]
                collection = memory_collection_name(tenant_id, self._embed.model_id)
                await self._vector_store.upsert_memory(
                    collection=collection,
                    memory_id=mem.id,
                    embedding=embedding,
                    content=content,
                    metadata={
                        "tenant_id": tenant_id,
                        "category_id": category_id,
                        "kind": MemoryKind.EPISODIC.value,
                        "source_job_id": job_id,
                        "is_active": True,
                    },
                )

                # Persist to Mongo with the embedding ID.
                mem_with_emb = mem.model_copy(update={"content_embedding_id": mem.id})
                await self._memory_repo.insert(mem_with_emb)

                span.set_attribute(Attr.MEMORY_ID, mem.id)
                span.set_attribute(Attr.MEMORY_KIND, MemoryKind.EPISODIC.value)
                span.set_attribute(Attr.MEMORY_CONFIDENCE, mem.confidence)
                logger.info(
                    "memory.episodic.written",
                    memory_id=mem.id,
                    job_id=job_id,
                    tenant_id=tenant_id,
                )

                try:
                    METRICS.memory_writes.labels(
                        tenant_id=tenant_id,
                        kind=MemoryKind.EPISODIC.value,
                        status=StatusLabel.SUCCESS,
                    ).inc()
                except Exception:
                    pass

                return mem_with_emb

            except Exception as exc:
                logger.error(
                    "memory.episodic.error",
                    job_id=job_id,
                    tenant_id=tenant_id,
                    error=str(exc),
                )
                try:
                    METRICS.memory_writes.labels(
                        tenant_id=tenant_id,
                        kind=MemoryKind.EPISODIC.value,
                        status=StatusLabel.ERROR,
                    ).inc()
                except Exception:
                    pass
                raise
            finally:
                elapsed = time.perf_counter() - t0
                try:
                    METRICS.memory_consolidation_duration.labels(
                        tenant_id=tenant_id,
                        status=StatusLabel.SUCCESS,
                    ).observe(elapsed)
                except Exception:
                    pass
