"""MongoDB BM25 retriever using the $text search index.

Mongo's $text search is not a pure BM25 implementation — it uses a variant of
TF-IDF with phrase proximity and stop-word filtering. For our purposes it is
close enough and is the most practical option without adding a dedicated search
engine. The chunks__text_bm25 index (defined in infrastructure/mongo/indexes.py)
covers the `text` field on the chunks collection.

Design notes:
    - Filtering by (tenant_id, category_id) at query time ensures cross-tenant
      and cross-category bleed is impossible at the storage layer, not just in
      application logic.
    - $text search and tenant_id/category_id filters are evaluated together by
      Mongo using the text index + compound predicates. The planner uses the text
      index for text scoring and applies the other filters as post-index filters
      since Mongo text indexes cannot be compound with other fields (as of 7.x).
      This means the limit is applied AFTER filtering, so we fetch top_k from
      the scored set that also matches tenant/category.
    - parent_text is NOT fetched here. Parent expansion is the HybridRetriever's
      responsibility, done after RRF merge and reranking, so we only expand the
      final top-K rather than every candidate.
    - source_url is left None; the Analyst enriches it from the Source collection
      when building Citation objects.
"""

from __future__ import annotations

from opentelemetry import trace

from trendstorm.domain.retrieval.models import RetrievalRequest, RetrievedChunk
from trendstorm.infrastructure.mongo.client import MongoClient
from trendstorm.infrastructure.mongo.repositories._base import TenantScopedRepository
from trendstorm.infrastructure.mongo.schema import Collection
from trendstorm.shared.errors import DatabaseError
from trendstorm.shared.logging import get_logger

try:
    from pymongo.errors import PyMongoError
except ImportError:
    PyMongoError = Exception  # type: ignore[misc,assignment]

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)

# Projection fields needed from a Chunk document for retrieval.
_CHUNK_PROJECTION = {
    "_id": 1,
    "text": 1,
    "document_id": 1,
    "source_id": 1,
    "parent_chunk_id": 1,
    # textScore is a computed meta field — included via the meta operator below.
}


class MongoBM25Retriever:
    """BM25 retriever backed by MongoDB $text search.

    Satisfies the BM25Retriever Protocol structurally — no inheritance needed.
    """

    def __init__(self, mongo: MongoClient) -> None:
        self._mongo = mongo

    async def retrieve(self, request: RetrievalRequest) -> list[RetrievedChunk]:
        """Query the chunks $text index, return scored results.

        The returned list is ordered by descending textScore. parent_text and
        source_url are always None here; the HybridRetriever fills them in.
        """
        with tracer.start_as_current_span("retrieval.bm25") as span:
            span.set_attribute("retrieval.query", request.query[:200])
            span.set_attribute("retrieval.tenant_id", request.tenant_id)
            span.set_attribute("retrieval.category_id", request.category_id)
            span.set_attribute("retrieval.top_k", request.top_k)

            results = await self._run_query(request)
            span.set_attribute("retrieval.result_count", len(results))
            return results

    async def _run_query(self, request: RetrievalRequest) -> list[RetrievedChunk]:
        coll = self._mongo.db[Collection.CHUNKS.value]

        # _tenant_query is the single authoritative filter builder (CLAUDE.md rule 3).
        query = TenantScopedRepository._tenant_query(
            request.tenant_id,
            category_id=request.category_id,
            **{"$text": {"$search": request.query}},
        )

        projection = {
            **_CHUNK_PROJECTION,
            "score": {"$meta": "textScore"},
        }

        try:
            cursor = (
                coll.find(query, projection)
                .sort([("score", {"$meta": "textScore"})])
                .limit(request.top_k)
            )
            docs = await cursor.to_list(length=request.top_k)
        except PyMongoError as exc:
            raise DatabaseError(
                "BM25 retrieval failed",
                context={
                    "error": str(exc),
                    "query": request.query[:200],
                    "tenant_id": request.tenant_id,
                    "category_id": request.category_id,
                },
            ) from exc

        chunks = []
        for doc in docs:
            chunks.append(
                RetrievedChunk(
                    chunk_id=str(doc["_id"]),
                    score=float(doc.get("score", 0.0)),
                    text=doc["text"],
                    parent_text=None,  # filled by HybridRetriever
                    document_id=doc["document_id"],
                    source_id=doc["source_id"],
                    source_url=None,  # filled by Analyst
                )
            )

        logger.debug(
            "bm25_retrieve_done",
            query=request.query[:100],
            tenant_id=request.tenant_id,
            category_id=request.category_id,
            result_count=len(chunks),
        )
        return chunks
