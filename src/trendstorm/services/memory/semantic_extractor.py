"""Semantic memory extractor — extracts durable factual claims from an analysis.

Called after a successful publish (episodic write already done). Uses a
lightweight LLM (Haiku / Flash-class) to distil N factual claims from the
analysis text, detects contradictions with existing active memories, and
persists new + superseded memories.

HITL gating is NOT enforced here — the caller (MemoryConsolidationWorker)
decides whether to run this step based on tenant settings. When hitl_mode
is "always", the worker defers this step until after review; when "off",
it runs immediately post-publish.
"""
from __future__ import annotations

import importlib.resources
import time
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field

from trendstorm.domain.memories.models import Memory, MemoryKind, MemorySource
from trendstorm.infrastructure.vectors.chroma_memory_store import (
    ChromaMemoryStore,
    memory_collection_name,
)
from trendstorm.shared.errors import LLMSchemaError
from trendstorm.shared.ids import new_id
from trendstorm.shared.logging import get_logger
from trendstorm.shared.metrics.registry import METRICS, StatusLabel
from trendstorm.shared.tracing.semantics import Attr

if TYPE_CHECKING:
    from trendstorm.domain.analyses.models import Analysis
    from trendstorm.domain.llm.models import Message
    from trendstorm.domain.llm.providers import EmbeddingProvider, StructuredChatProvider
    from trendstorm.domain.memories.repository import MemoryRepository

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)

_TOOL_NAME = "record_memories"
_TOOL_SCHEMA: dict[str, Any] = {
    "name": _TOOL_NAME,
    "description": (
        "Record durable factual claims distilled from the trend analysis. "
        "Each claim must be directly supported by the analysis text."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "memories": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "string"},
                        "confidence": {"type": "number"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["claim", "confidence"],
                },
            }
        },
        "required": ["memories"],
    },
}

# Cosine similarity above this threshold triggers a supersede check.
_DEFAULT_SUPERSEDE_THRESHOLD = 0.92
# Maximum semantic memories written per job (cost control).
_DEFAULT_MAX_MEMORIES = 8


def _load_prompt() -> str:
    pkg = importlib.resources.files("trendstorm.services.memory.prompts")
    return (pkg / "memory_extraction_system.md").read_text(encoding="utf-8").strip()


class SemanticMemoryExtractor:
    """Extracts and persists semantic memories from a completed analysis.

    Supersede detection:
        For each new claim, the extractor queries ChromaDB for the most
        similar existing active semantic memory in the same category. If
        the similarity exceeds `supersede_threshold`, the old memory is
        marked superseded and the new one takes its place. No LLM required
        for the supersede decision — cosine similarity alone is sufficient
        because both claims are in the same embedding space.
    """

    def __init__(
        self,
        chat_provider: StructuredChatProvider,
        embed: EmbeddingProvider,
        vector_store: ChromaMemoryStore,
        memory_repo: MemoryRepository,
        *,
        supersede_threshold: float = _DEFAULT_SUPERSEDE_THRESHOLD,
        max_memories_per_job: int = _DEFAULT_MAX_MEMORIES,
        _prompt_text: str | None = None,
    ) -> None:
        self._chat = chat_provider
        self._embed = embed
        self._vector_store = vector_store
        self._memory_repo = memory_repo
        self._supersede_threshold = supersede_threshold
        self._max_memories = max_memories_per_job
        self._prompt: str = _prompt_text if _prompt_text is not None else _load_prompt()

    async def extract_and_store(
        self,
        *,
        analysis: Analysis,
        tenant_id: str,
        job_id: str,
        category_id: str,
    ) -> list[Memory]:
        """Run extraction and persist memories. Returns the list of created Memory docs."""
        with tracer.start_as_current_span(
            "memory.semantic.extract",
            attributes={
                Attr.TENANT_ID: tenant_id,
                Attr.JOB_ID: job_id,
                Attr.CATEGORY_ID: category_id,
                Attr.MODEL_ID: self._chat.model_id,
            },
        ) as span:
            t0 = time.perf_counter()
            created: list[Memory] = []
            try:
                raw_claims = await self._call_llm(analysis, tenant_id, job_id)
                if not raw_claims:
                    return []

                for raw in raw_claims[: self._max_memories]:
                    claim = str(raw.get("claim", "")).strip()
                    confidence = float(raw.get("confidence", 0.5))
                    tags = list(raw.get("tags") or [])

                    if not claim or confidence < 0.5:
                        continue

                    mem = Memory(
                        tenant_id=tenant_id,
                        category_id=category_id,
                        kind=MemoryKind.SEMANTIC,
                        source=MemorySource.EXTRACTED,
                        content=claim,
                        confidence=confidence,
                        source_job_id=job_id,
                        source_analysis_id=analysis.id,
                        tags=tags,
                    )

                    # Embed.
                    embeddings = await self._embed.embed_batch([claim])
                    embedding = embeddings[0]
                    collection = memory_collection_name(tenant_id, self._embed.model_id)

                    # Supersede detection — check similarity to existing active semantics.
                    await self._maybe_supersede(
                        embedding=embedding,
                        collection=collection,
                        new_memory_id=mem.id,
                        tenant_id=tenant_id,
                        category_id=category_id,
                    )

                    # Upsert to ChromaDB.
                    await self._vector_store.upsert_memory(
                        collection=collection,
                        memory_id=mem.id,
                        embedding=embedding,
                        content=claim,
                        metadata={
                            "tenant_id": tenant_id,
                            "category_id": category_id,
                            "kind": MemoryKind.SEMANTIC.value,
                            "source_job_id": job_id,
                            "is_active": True,
                        },
                    )

                    mem_with_emb = mem.model_copy(update={"content_embedding_id": mem.id})
                    await self._memory_repo.insert(mem_with_emb)
                    created.append(mem_with_emb)

                    try:
                        METRICS.memory_writes.labels(
                            tenant_id=tenant_id,
                            kind=MemoryKind.SEMANTIC.value,
                            status=StatusLabel.SUCCESS,
                        ).inc()
                    except Exception:
                        pass

                span.set_attribute(Attr.MEMORY_WRITE_COUNT, len(created))
                logger.info(
                    "memory.semantic.written",
                    n=len(created),
                    job_id=job_id,
                    tenant_id=tenant_id,
                )
                return created

            except Exception as exc:
                logger.error(
                    "memory.semantic.error",
                    job_id=job_id,
                    tenant_id=tenant_id,
                    error=str(exc),
                )
                try:
                    METRICS.memory_writes.labels(
                        tenant_id=tenant_id,
                        kind=MemoryKind.SEMANTIC.value,
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
                        status=StatusLabel.SUCCESS if created else StatusLabel.ERROR,
                    ).observe(elapsed)
                except Exception:
                    pass

    async def _call_llm(
        self, analysis: Analysis, tenant_id: str, job_id: str
    ) -> list[dict[str, Any]]:
        from trendstorm.domain.llm.models import Message  # deferred

        user_content = (
            "## Analysis to distil\n\n"
            f"**Summary:** {analysis.summary}\n\n"
        )
        if analysis.insights:
            user_content += "**Key insights:**\n"
            for ins in analysis.insights[:10]:
                user_content += f"- {ins.claim} (confidence={ins.confidence:.2f})\n"
        user_content += "\n\nCall `record_memories` with the durable factual claims."

        messages: list[Message] = [
            Message(role="system", content=self._prompt),
            Message(role="user", content=user_content),
        ]
        tool_name, args, _tokens = await self._chat.complete_with_tools(
            messages,
            tools=[_TOOL_SCHEMA],
            tool_choice=_TOOL_NAME,
        )
        if tool_name != _TOOL_NAME:
            raise LLMSchemaError(
                f"Memory extractor returned unexpected tool: {tool_name!r}",
                context={"expected": _TOOL_NAME, "received": tool_name},
            )
        return list(args.get("memories", []) or [])

    async def _maybe_supersede(
        self,
        *,
        embedding: list[float],
        collection: str,
        new_memory_id: str,
        tenant_id: str,
        category_id: str,
    ) -> None:
        """Check if any existing active semantic memory is highly similar and supersede it."""
        hits = await self._vector_store.query_memories(
            collection=collection,
            query_embedding=embedding,
            n_results=1,
            tenant_id=tenant_id,
            category_id=category_id,
            kind=MemoryKind.SEMANTIC.value,
        )
        if hits and hits[0].score >= self._supersede_threshold:
            old_id = hits[0].id
            if old_id != new_memory_id:
                await self._memory_repo.supersede(tenant_id, old_id, new_memory_id)
                logger.info(
                    "memory.supersede",
                    old_id=old_id,
                    new_id=new_memory_id,
                    similarity=hits[0].score,
                )
