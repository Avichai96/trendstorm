"""Retrieval Protocols — the only interfaces the domain exposes to infrastructure.

Why separate BM25Retriever and VectorRetriever from Retriever?
    Both share the same method signature (`retrieve`), but typing them separately
    lets HybridRetriever's constructor enforce that callers wire the right backend
    to the right role. Without the tags, a caller could accidentally pass two BM25
    retrievers and get no type error.

Why CrossEncoderReranker is NOT a Retriever:
    Rerankers do not originate results — they re-score an existing candidate list.
    The input type (list[RetrievedChunk]) is different from RetrievalRequest,
    so the signature is genuinely different and merging them would be wrong.

Protocol vs ABC:
    @runtime_checkable Protocols allow `isinstance()` checks in tests and in the
    HybridRetriever to verify that concrete implementations satisfy the interface
    structurally. No inheritance required in concrete classes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from trendstorm.domain.retrieval.models import RetrievalRequest, RetrievedChunk


@runtime_checkable
class Retriever(Protocol):
    """Async interface for any single-backend retrieval implementation."""

    async def retrieve(self, request: RetrievalRequest) -> list[RetrievedChunk]:
        """Return ranked results for the given request.

        Results must be ordered by descending relevance score.
        Returns an empty list if no results are found (never raises for empty).
        """
        ...


@runtime_checkable
class BM25Retriever(Protocol):
    """Structural tag for BM25 (Mongo $text) retrieval implementations.

    Satisfies the same interface as Retriever. The separate type lets
    HybridRetriever's constructor enforce correct wiring.
    """

    async def retrieve(self, request: RetrievalRequest) -> list[RetrievedChunk]: ...


@runtime_checkable
class VectorRetriever(Protocol):
    """Structural tag for dense-vector (ChromaDB cosine) retrieval implementations."""

    async def retrieve(self, request: RetrievalRequest) -> list[RetrievedChunk]: ...


@runtime_checkable
class CrossEncoderReranker(Protocol):
    """Async interface for cross-encoder reranking.

    The reranker does NOT retrieve new results — it re-scores and filters a
    candidate list produced by an upstream retriever / RRF merge step.
    """

    async def rerank(
        self,
        query: str,
        candidates: list[RetrievedChunk],
        *,
        top_k: int,
    ) -> list[RetrievedChunk]:
        """Re-score candidates and return the top_k most relevant.

        The returned list is ordered by descending reranker score and has at most
        top_k elements (fewer if len(candidates) < top_k).
        """
        ...
