"""Cohere cross-encoder reranker (rerank-v3.5).

Uses Cohere's rerank API as a cross-encoder: it re-scores a candidate list by
jointly encoding the query and each document, yielding a more accurate relevance
signal than the bi-encoder scores from BM25 or vector search.

Funnel role:
    RRF merge → top-30 candidates → CohereReranker → top-10 final results.

Fallback contract (enforced by HybridRetriever, not here):
    If this class raises, HybridRetriever catches it, logs a warning, and falls
    back to the RRF-ranked top-K. This class does NOT implement that fallback —
    it raises on any Cohere API failure so the caller can decide.

Lifecycle:
    connect() creates the AsyncClientV2 (idempotent).
    close()   releases the underlying httpx client.
    health_check() returns True if the client is initialised (no network call —
    Cohere has no free "ping" endpoint that wouldn't consume quota).

Dependency group: llm (cohere>=5.13.0 is already present).
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from opentelemetry import trace

from trendstorm.domain.llm.errors import (
    LLMPermanentError,
    LLMRateLimitError,
    LLMTransientError,
)
from trendstorm.domain.retrieval.models import RetrievedChunk
from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)


def _map_cohere_error(exc: Exception) -> None:
    """Map Cohere SDK exceptions to domain LLM error hierarchy. Always raises."""
    try:
        import cohere.errors as _ce

        if isinstance(exc, _ce.TooManyRequestsError):
            raise LLMRateLimitError(f"Cohere rate limit: {exc}") from exc
        if isinstance(exc, _ce.UnauthorizedError):
            raise LLMPermanentError(f"Cohere auth failed: {exc}") from exc
        if isinstance(exc, _ce.BadRequestError):
            raise LLMPermanentError(f"Cohere bad request: {exc}") from exc
        if isinstance(exc, _ce.InternalServerError):
            raise LLMTransientError(f"Cohere server error: {exc}") from exc
    except ImportError:
        pass

    msg = str(exc).lower()
    if "429" in msg or "rate" in msg or "quota" in msg:
        raise LLMRateLimitError(str(exc)) from exc
    if "401" in msg or "403" in msg or "auth" in msg:
        raise LLMPermanentError(str(exc)) from exc
    raise LLMTransientError(str(exc)) from exc


class CohereReranker:
    """Cross-encoder reranker backed by Cohere rerank-v3.5.

    Satisfies the CrossEncoderReranker Protocol structurally.

    Args:
        api_key   — Cohere API key (use SecretStr.get_secret_value() at call site).
        model     — Cohere rerank model name. Default: "rerank-v3.5".
        _client   — Inject a fake client for unit tests; leave None for production.

    """

    def __init__(
        self,
        api_key: str,
        model: str = "rerank-v3.5",
        *,
        _client: Any = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._client: Any = _client

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Initialise the Cohere async client. Idempotent."""
        if self._client is not None:
            return
        import cohere

        self._client = cohere.AsyncClientV2(api_key=self._api_key)
        logger.info("cohere.connected", model=self._model)

    async def close(self) -> None:
        """Release the underlying httpx client."""
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.close()
        self._client = None

    async def health_check(self) -> bool:
        """Return True if the client is initialised. No network call (no free ping endpoint)."""
        return self._client is not None

    # ------------------------------------------------------------------
    # CrossEncoderReranker Protocol
    # ------------------------------------------------------------------

    async def rerank(
        self,
        query: str,
        candidates: list[RetrievedChunk],
        *,
        top_k: int,
    ) -> list[RetrievedChunk]:
        """Re-score candidates with the Cohere cross-encoder, return top_k.

        The returned list is ordered by descending relevance_score. Each
        RetrievedChunk's score field is replaced with the reranker's score.
        All other fields (text, parent_text, provenance) are preserved.
        """
        if not candidates:
            return []

        effective_top_k = min(top_k, len(candidates))

        with tracer.start_as_current_span("retrieval.rerank") as span:
            span.set_attribute("retrieval.query", query[:200])
            span.set_attribute("retrieval.candidates", len(candidates))
            span.set_attribute("retrieval.top_k", effective_top_k)
            span.set_attribute("retrieval.model", self._model)

            results = await self._call_cohere(query, candidates, effective_top_k)
            span.set_attribute("retrieval.reranked_count", len(results))
            return results

    async def _call_cohere(
        self,
        query: str,
        candidates: list[RetrievedChunk],
        top_k: int,
    ) -> list[RetrievedChunk]:
        texts = [c.text for c in candidates]
        try:
            response = await self._client.rerank(
                model=self._model,
                query=query,
                documents=texts,
                top_n=top_k,
            )
        except (LLMRateLimitError, LLMTransientError, LLMPermanentError):
            raise
        except Exception as exc:
            _map_cohere_error(exc)

        reranked: list[RetrievedChunk] = []
        for item in response.results:
            original = candidates[item.index]
            reranked.append(
                RetrievedChunk(
                    chunk_id=original.chunk_id,
                    score=float(item.relevance_score),
                    text=original.text,
                    parent_text=original.parent_text,
                    document_id=original.document_id,
                    source_id=original.source_id,
                    source_url=original.source_url,
                )
            )

        logger.debug(
            "cohere_rerank_done",
            query=query[:100],
            candidates=len(candidates),
            reranked=len(reranked),
            model=self._model,
        )
        return reranked
