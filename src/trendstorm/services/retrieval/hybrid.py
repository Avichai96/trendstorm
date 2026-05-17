"""HybridRetriever — the full retrieval pipeline.

Pipeline stages (in order):
    1. Query expansion   — 1 query → N sub-queries via LLM
    2. Concurrent fetch  — BM25 + vector retrieval for each sub-query in parallel
    3. RRF merge         — union all ranked lists; take top rerank_k candidates
    4. Reranking         — Cohere cross-encoder on the candidates; fallback to RRF order
    5. Parent expansion  — fetch parent chunk text from Mongo for final top-K results

Concurrency model:
    asyncio.gather over (2 * N) retrieval tasks, where N = query_expansion_count.
    Bounded by settings — never an unbounded fan-out. Individual backend failures
    log a warning and are excluded from the RRF input; the pipeline continues.

Reranker fallback:
    If the reranker is None (Cohere not configured) or raises, the pipeline falls
    back to RRF-sorted top-K. This is always logged at WARNING level.

Parent expansion:
    Two Mongo batch lookups per retrieval call (one for child chunks, one for
    parents). Cost is proportional to final_k, not to the candidate pool size,
    because expansion happens AFTER reranking.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from opentelemetry import trace

from trendstorm.domain.retrieval.models import RetrievalRequest, RetrievedChunk
from trendstorm.infrastructure.mongo.repositories import MongoChunkRepository
from trendstorm.services.retrieval.rrf import rrf
from trendstorm.shared.logging import get_logger
from trendstorm.shared.metrics.registry import METRICS
from trendstorm.shared.tracing.semantics import Attr

if TYPE_CHECKING:
    from trendstorm.domain.retrieval.protocols import (
        BM25Retriever,
        CrossEncoderReranker,
        VectorRetriever,
    )
    from trendstorm.infrastructure.mongo.client import MongoClient
    from trendstorm.services.retrieval.query_expansion import QueryExpander
    from trendstorm.shared.config import AnalysisSettings

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)


class HybridRetriever:
    """Orchestrates BM25 + vector retrieval, RRF fusion, reranking, and parent expansion.

    Args:
        bm25       — BM25 retriever (Mongo $text)
        vector     — Dense vector retriever (ChromaDB cosine)
        expander   — Query expander (LLM-based sub-query generation)
        mongo      — MongoClient for parent chunk lookups
        settings   — AnalysisSettings controlling funnel widths
        reranker   — Optional cross-encoder; if None, RRF order is used as-is

    """

    def __init__(
        self,
        bm25: BM25Retriever,
        vector: VectorRetriever,
        expander: QueryExpander,
        mongo: MongoClient,
        settings: AnalysisSettings,
        *,
        reranker: CrossEncoderReranker | None = None,
    ) -> None:
        self._bm25 = bm25
        self._vector = vector
        self._expander = expander
        self._chunk_repo = MongoChunkRepository(mongo)
        self._settings = settings
        self._reranker = reranker

    async def retrieve(self, request: RetrievalRequest) -> list[RetrievedChunk]:
        """Run the full hybrid retrieval pipeline for the given request."""
        with tracer.start_as_current_span("retrieval.hybrid") as span:
            span.set_attribute(Attr.QUERY, request.query[:200])
            span.set_attribute(Attr.TENANT_ID, request.tenant_id)
            span.set_attribute(Attr.CATEGORY_ID, request.category_id)
            span.set_attribute(Attr.RETRIEVAL_K, self._settings.final_k)

            results = await self._run(request)
            span.set_attribute(Attr.AFTER_RERANK_COUNT, len(results))
            return results

    async def _run(self, request: RetrievalRequest) -> list[RetrievedChunk]:
        # ------------------------------------------------------------------ #
        # 1. Query expansion
        # ------------------------------------------------------------------ #
        with tracer.start_as_current_span("retrieval.hybrid.expand"):
            sub_queries = await self._expander.expand(
                request.query,
                count=self._settings.query_expansion_count,
            )
        logger.debug("hybrid_sub_queries", count=len(sub_queries), queries=[q[:80] for q in sub_queries])

        # ------------------------------------------------------------------ #
        # 2. Concurrent retrieval — BM25 + vector per sub-query
        # ------------------------------------------------------------------ #
        sub_requests = [
            RetrievalRequest(
                query=q,
                tenant_id=request.tenant_id,
                category_id=request.category_id,
                top_k=self._settings.retrieval_k,
            )
            for q in sub_queries
        ]

        tasks = [
            coro
            for sub_req in sub_requests
            for coro in (
                self._bm25.retrieve(sub_req),
                self._vector.retrieve(sub_req),
            )
        ]

        with tracer.start_as_current_span("retrieval.hybrid.fetch") as span:
            span.set_attribute("trendstorm.task_count", len(tasks))
            raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        # ------------------------------------------------------------------ #
        # 3. RRF merge — record per-backend hit counts as metrics
        # ------------------------------------------------------------------ #
        with tracer.start_as_current_span("retrieval.hybrid.rrf") as span:
            ranked_lists: list[list[str]] = []
            chunk_map: dict[str, RetrievedChunk] = {}

            # Tasks are interleaved: (bm25_q0, vec_q0, bm25_q1, vec_q1, ...)
            bm25_total = 0
            vector_total = 0
            for i, result in enumerate(raw_results):
                backend = "bm25" if i % 2 == 0 else "vector"
                if isinstance(result, BaseException):
                    logger.warning("retrieval_backend_error", error=str(result), backend=backend)
                    continue
                hit_count = len(result) if result else 0
                if backend == "bm25":
                    bm25_total += hit_count
                else:
                    vector_total += hit_count
                if result:
                    ranked_lists.append([c.chunk_id for c in result])
                    for chunk in result:
                        existing = chunk_map.get(chunk.chunk_id)
                        if existing is None or chunk.score > existing.score:
                            chunk_map[chunk.chunk_id] = chunk

            # Record retrieval funnel hit counts as metrics (bounded cardinality).
            with contextlib.suppress(Exception):
                METRICS.analyst_retrieval_hits.labels(
                    tenant_id=request.tenant_id, backend="bm25"
                ).observe(bm25_total)
                METRICS.analyst_retrieval_hits.labels(
                    tenant_id=request.tenant_id, backend="vector"
                ).observe(vector_total)

            span.set_attribute(Attr.BM25_HITS, bm25_total)
            span.set_attribute(Attr.VECTOR_HITS, vector_total)

            if not chunk_map:
                logger.warning("hybrid_no_results", query=request.query[:100])
                return []

            rrf_scores = rrf(ranked_lists)
            sorted_ids = sorted(rrf_scores, key=lambda cid: rrf_scores[cid], reverse=True)
            span.set_attribute(Attr.AFTER_RRF_COUNT, len(sorted_ids))

        # Build the candidate list (top rerank_k), updating score to RRF value.
        candidates: list[RetrievedChunk] = []
        for cid in sorted_ids[: self._settings.rerank_k]:
            if cid not in chunk_map:
                continue
            orig = chunk_map[cid]
            candidates.append(
                RetrievedChunk(
                    chunk_id=orig.chunk_id,
                    score=rrf_scores[cid],
                    text=orig.text,
                    parent_text=orig.parent_text,
                    document_id=orig.document_id,
                    source_id=orig.source_id,
                    source_url=orig.source_url,
                )
            )

        logger.debug("hybrid_rrf_done", candidates=len(candidates))

        # ------------------------------------------------------------------ #
        # 4. Reranking (optional — falls back to RRF order on failure)
        # ------------------------------------------------------------------ #
        with tracer.start_as_current_span("retrieval.hybrid.rerank") as span:
            if self._reranker is not None:
                try:
                    candidates = await self._reranker.rerank(
                        request.query,
                        candidates,
                        top_k=self._settings.final_k,
                    )
                    span.set_attribute(Attr.RERANKER_USED, "cohere")
                except Exception as exc:
                    logger.warning(
                        "cohere_rerank_failed_rrf_fallback",
                        error=str(exc),
                        candidates=len(candidates),
                    )
                    candidates = candidates[: self._settings.final_k]
                    span.set_attribute(Attr.RERANKER_USED, "rrf_fallback")
            else:
                candidates = candidates[: self._settings.final_k]
                span.set_attribute(Attr.RERANKER_USED, "none")
            span.set_attribute(Attr.AFTER_RERANK_COUNT, len(candidates))

        logger.debug("hybrid_after_rerank", final_candidates=len(candidates))

        # ------------------------------------------------------------------ #
        # 5. Parent expansion — two Mongo batch lookups, final_k sized
        # ------------------------------------------------------------------ #
        with tracer.start_as_current_span("retrieval.hybrid.parent_expand"):
            candidates = await self._expand_parents(request.tenant_id, candidates)

        return candidates

    async def _expand_parents(
        self,
        tenant_id: str,
        chunks: list[RetrievedChunk],
    ) -> list[RetrievedChunk]:
        """Fetch parent chunk text for child chunks; attach as parent_text."""
        if not chunks:
            return chunks

        # First batch: fetch the final chunks to learn their parent_chunk_id.
        chunk_docs = await self._chunk_repo.get_many(
            tenant_id, [c.chunk_id for c in chunks]
        )
        doc_map = {d.id: d for d in chunk_docs}

        parent_ids = list({
            d.parent_chunk_id
            for d in doc_map.values()
            if d.parent_chunk_id is not None
        })

        parent_map: dict[str, str] = {}  # parent_id → parent text
        if parent_ids:
            parent_docs = await self._chunk_repo.get_many(tenant_id, parent_ids)
            parent_map = {d.id: d.text for d in parent_docs}

        result: list[RetrievedChunk] = []
        for chunk in chunks:
            doc = doc_map.get(chunk.chunk_id)
            parent_text: str | None = None
            if doc and doc.parent_chunk_id:
                parent_text = parent_map.get(doc.parent_chunk_id)
            result.append(
                RetrievedChunk(
                    chunk_id=chunk.chunk_id,
                    score=chunk.score,
                    text=chunk.text,
                    parent_text=parent_text,
                    document_id=chunk.document_id,
                    source_id=chunk.source_id,
                    source_url=chunk.source_url,
                )
            )

        return result
