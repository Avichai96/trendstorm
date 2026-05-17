"""Retrieval domain — models and Protocols for hybrid search.

The retrieval pipeline combines BM25 (Mongo $text) and dense vector search
(ChromaDB cosine) via Reciprocal Rank Fusion, then optionally reranks with a
cross-encoder. Results carry enough provenance to build Citation objects in the
Analysis without additional DB lookups.

Protocols:
    Retriever            — base async interface for any retrieval backend
    BM25Retriever        — structural tag for BM25 implementations
    VectorRetriever      — structural tag for dense-vector implementations
    CrossEncoderReranker — reranks a candidate list using a cross-encoder model

Models:
    RetrievalRequest  — query + filter parameters
    RetrievedChunk    — one retrieval result with text, parent text, provenance
"""
from trendstorm.domain.retrieval.models import RetrievalRequest, RetrievedChunk
from trendstorm.domain.retrieval.protocols import (
    BM25Retriever,
    CrossEncoderReranker,
    Retriever,
    VectorRetriever,
)

__all__ = [
    "BM25Retriever",
    "CrossEncoderReranker",
    "RetrievalRequest",
    "RetrievedChunk",
    "Retriever",
    "VectorRetriever",
]
