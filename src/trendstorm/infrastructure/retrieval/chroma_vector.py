"""ChromaDB vector retriever for dense similarity search.

Wraps ChromaVectorStore.query (Phase 7) with the EmbeddingProvider to form a
self-contained VectorRetriever: given a RetrievalRequest, it embeds the query
and returns ranked RetrievedChunk objects.

Design notes:
    - task_type="query" is passed to the embedding provider so asymmetric
      models (Gemini RETRIEVAL_QUERY) use the correct model weights.
    - Chunk text is retrieved from ChromaDB's documents field (stored at upsert
      time by KnowledgePipeline). No secondary Mongo round-trip needed at query
      time; the Chroma document field is the convenient cache of child text.
    - parent_text and source_url are left None; HybridRetriever fills them in
      after RRF merge and reranking, so parent expansion only costs N fetches
      for the final top-K, not for every candidate.
    - Filtering: Chroma requires $and for multi-field metadata filters. Both
      tenant_id and category_id are mandatory — see CLAUDE.md rule 4.
    - If VectorHit.document is None (shouldn't happen after Phase 7 upserts),
      the chunk is dropped with a warning rather than surfacing an empty-text
      result to the LLM.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from opentelemetry import trace

from trendstorm.domain.retrieval.models import RetrievalRequest, RetrievedChunk
from trendstorm.infrastructure.vectors.chroma_store import ChromaVectorStore
from trendstorm.shared.errors import DatabaseError
from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    from trendstorm.domain.llm.providers import EmbeddingProvider

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)


def _collection_name(tenant_id: str, model_id: str) -> str:
    """Build a Chroma collection name for a given tenant + embedding model.

    Must match the naming used by KnowledgePipeline (agents/knowledge/pipeline.py).
    Format: chunks__{tenant_short}__{model_id_safe}
    """
    safe = model_id.replace(".", "_").replace("-", "_")
    return f"chunks__{tenant_id[:8].lower()}__{safe}"


def _chroma_where(tenant_id: str, category_id: str) -> dict[str, object]:
    """Build a Chroma $and filter for tenant + category isolation.

    Chroma requires $and for multiple field conditions. Single-field filters
    can use the shorthand dict, but two fields must use the $and operator.
    """
    return {
        "$and": [
            {"tenant_id": {"$eq": tenant_id}},
            {"category_id": {"$eq": category_id}},
        ]
    }


class ChromaVectorRetriever:
    """Dense vector retriever backed by ChromaDB.

    Satisfies the VectorRetriever Protocol structurally — no inheritance needed.

    Constructor args:
        vector_store       — connected ChromaVectorStore (from app.state)
        embedding_provider — provider used at query time with task_type="query"
    """

    def __init__(
        self,
        vector_store: ChromaVectorStore,
        embedding_provider: EmbeddingProvider,
    ) -> None:
        self._store = vector_store
        self._embed = embedding_provider

    async def retrieve(self, request: RetrievalRequest) -> list[RetrievedChunk]:
        """Embed the query and run a cosine similarity search against ChromaDB."""
        with tracer.start_as_current_span("retrieval.vector") as span:
            span.set_attribute("retrieval.query", request.query[:200])
            span.set_attribute("retrieval.tenant_id", request.tenant_id)
            span.set_attribute("retrieval.category_id", request.category_id)
            span.set_attribute("retrieval.top_k", request.top_k)
            span.set_attribute("retrieval.model_id", self._embed.model_id)

            results = await self._run_query(request)
            span.set_attribute("retrieval.result_count", len(results))
            return results

    async def _run_query(self, request: RetrievalRequest) -> list[RetrievedChunk]:
        # Embed with task_type="query" — asymmetric models use different weights.
        try:
            embed_result = await self._embed.embed_batch(
                [request.query], task_type="query"
            )
        except Exception as exc:
            raise DatabaseError(
                "Vector retrieval failed during query embedding",
                context={"error": str(exc), "model_id": self._embed.model_id},
            ) from exc

        if not embed_result.vectors:
            return []

        query_embedding = embed_result.vectors[0]
        collection = _collection_name(request.tenant_id, self._embed.model_id)
        where = _chroma_where(request.tenant_id, request.category_id)

        try:
            hits = await self._store.query(
                collection,
                query_embedding,
                n_results=request.top_k,
                where=where,
            )
        except Exception as exc:
            raise DatabaseError(
                "Vector retrieval failed during Chroma query",
                context={
                    "error": str(exc),
                    "collection": collection,
                    "tenant_id": request.tenant_id,
                    "category_id": request.category_id,
                },
            ) from exc

        chunks: list[RetrievedChunk] = []
        for hit in hits:
            if not hit.document:
                logger.warning(
                    "chroma_vector_hit_missing_text",
                    chunk_id=hit.id,
                    collection=collection,
                )
                continue
            chunks.append(
                RetrievedChunk(
                    chunk_id=hit.id,
                    score=hit.score,
                    text=hit.document,
                    parent_text=None,           # filled by HybridRetriever
                    document_id=hit.metadata.get("document_id", ""),
                    source_id=hit.metadata.get("source_id", ""),
                    source_url=None,            # filled by Analyst
                )
            )

        logger.debug(
            "vector_retrieve_done",
            query=request.query[:100],
            tenant_id=request.tenant_id,
            category_id=request.category_id,
            model_id=self._embed.model_id,
            result_count=len(chunks),
        )
        return chunks
