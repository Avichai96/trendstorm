"""Unit tests for CohereReranker.

All Cohere API calls are faked via an injected _client. No real network calls.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from trendstorm.domain.llm.errors import (
    LLMPermanentError,
    LLMRateLimitError,
    LLMTransientError,
)
from trendstorm.domain.retrieval.models import RetrievedChunk
from trendstorm.domain.retrieval.protocols import CrossEncoderReranker
from trendstorm.infrastructure.retrieval.cohere_reranker import CohereReranker
from trendstorm.shared.ids import new_id

# ---------------------------------------------------------------------------
# Fake Cohere client helpers
# ---------------------------------------------------------------------------

@dataclass
class FakeRerankResultItem:
    index: int
    relevance_score: float


@dataclass
class FakeRerankResponse:
    results: list[FakeRerankResultItem]


def _fake_client(results: list[FakeRerankResultItem]) -> Any:
    """Return a mock Cohere client whose rerank() returns the given results."""
    client = MagicMock()
    client.rerank = AsyncMock(return_value=FakeRerankResponse(results=results))
    client.close = AsyncMock()
    return client


def _make_candidate(
    text: str = "some text",
    score: float = 0.5,
    parent_text: str | None = None,
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=new_id(),
        score=score,
        text=text,
        parent_text=parent_text,
        document_id=new_id(),
        source_id=new_id(),
        source_url="https://example.com",
    )


def _make_reranker(results: list[FakeRerankResultItem]) -> CohereReranker:
    return CohereReranker(
        api_key="fake-key",
        _client=_fake_client(results),
    )


# ---------------------------------------------------------------------------
# Protocol satisfaction
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCohereRerankerProtocol:
    def test_satisfies_cross_encoder_reranker_protocol(self) -> None:
        r = CohereReranker(api_key="k", _client=MagicMock())
        assert isinstance(r, CrossEncoderReranker)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCohereRerankerLifecycle:
    async def test_connect_is_idempotent_with_injected_client(self) -> None:
        client = MagicMock()
        r = CohereReranker(api_key="k", _client=client)
        await r.connect()
        # Should not replace the injected client
        assert r._client is client

    async def test_health_check_true_when_client_set(self) -> None:
        r = CohereReranker(api_key="k", _client=MagicMock())
        assert await r.health_check() is True

    async def test_health_check_false_when_no_client(self) -> None:
        r = CohereReranker(api_key="k")
        assert await r.health_check() is False

    async def test_close_sets_client_to_none(self) -> None:
        client = MagicMock()
        client.close = AsyncMock()
        r = CohereReranker(api_key="k", _client=client)
        await r.close()
        assert r._client is None
        client.close.assert_called_once()

    async def test_close_tolerates_exception_from_client(self) -> None:
        client = MagicMock()
        client.close = AsyncMock(side_effect=RuntimeError("boom"))
        r = CohereReranker(api_key="k", _client=client)
        await r.close()  # must not propagate
        assert r._client is None


# ---------------------------------------------------------------------------
# Reranking logic
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCohereRerankerRerank:
    async def test_returns_top_k_in_score_order(self) -> None:
        candidates = [
            _make_candidate("doc A", score=0.3),
            _make_candidate("doc B", score=0.7),
            _make_candidate("doc C", score=0.5),
        ]
        # Cohere returns them re-ordered: B first, then A
        fake_results = [
            FakeRerankResultItem(index=1, relevance_score=0.95),
            FakeRerankResultItem(index=0, relevance_score=0.40),
        ]
        r = _make_reranker(fake_results)
        results = await r.rerank("my query", candidates, top_k=2)

        assert len(results) == 2
        assert results[0].text == "doc B"
        assert results[0].score == pytest.approx(0.95)
        assert results[1].text == "doc A"
        assert results[1].score == pytest.approx(0.40)

    async def test_score_is_replaced_with_reranker_score(self) -> None:
        candidate = _make_candidate("doc", score=0.1)
        fake_results = [FakeRerankResultItem(index=0, relevance_score=0.88)]
        r = _make_reranker(fake_results)
        results = await r.rerank("q", [candidate], top_k=1)
        assert results[0].score == pytest.approx(0.88)

    async def test_other_fields_are_preserved(self) -> None:
        candidate = _make_candidate("text", parent_text="parent context")
        assert hasattr(candidate, "source_url")  # confirm field exists
        fake_results = [FakeRerankResultItem(index=0, relevance_score=0.75)]
        r = _make_reranker(fake_results)
        results = await r.rerank("q", [candidate], top_k=1)
        assert results[0].chunk_id == candidate.chunk_id
        assert results[0].text == candidate.text
        assert results[0].parent_text == "parent context"
        assert results[0].document_id == candidate.document_id
        assert results[0].source_id == candidate.source_id
        assert results[0].source_url == candidate.source_url

    async def test_empty_candidates_returns_empty(self) -> None:
        r = _make_reranker([])
        results = await r.rerank("q", [], top_k=5)
        assert results == []
        # client.rerank must NOT be called for empty input
        r._client.rerank.assert_not_called()

    async def test_top_k_clamped_to_candidate_count(self) -> None:
        candidates = [_make_candidate(f"doc {i}") for i in range(3)]
        fake_results = [FakeRerankResultItem(index=i, relevance_score=0.5 - i * 0.1) for i in range(3)]
        r = _make_reranker(fake_results)
        # top_k=10 but only 3 candidates → Cohere receives top_n=3
        await r.rerank("q", candidates, top_k=10)
        call_kwargs = r._client.rerank.call_args.kwargs
        assert call_kwargs["top_n"] == 3

    async def test_cohere_called_with_correct_texts(self) -> None:
        candidates = [
            _make_candidate("first chunk"),
            _make_candidate("second chunk"),
        ]
        r = _make_reranker([FakeRerankResultItem(index=0, relevance_score=0.9)])
        await r.rerank("my query", candidates, top_k=1)
        call_kwargs = r._client.rerank.call_args.kwargs
        assert call_kwargs["query"] == "my query"
        assert call_kwargs["documents"] == ["first chunk", "second chunk"]
        assert call_kwargs["model"] == "rerank-v3.5"

    async def test_rate_limit_error_propagates(self) -> None:
        client = MagicMock()
        client.rerank = AsyncMock(side_effect=LLMRateLimitError("429"))
        client.close = AsyncMock()
        r = CohereReranker(api_key="k", _client=client)
        with pytest.raises(LLMRateLimitError):
            await r.rerank("q", [_make_candidate()], top_k=1)

    async def test_transient_error_propagates(self) -> None:
        client = MagicMock()
        client.rerank = AsyncMock(side_effect=LLMTransientError("503"))
        client.close = AsyncMock()
        r = CohereReranker(api_key="k", _client=client)
        with pytest.raises(LLMTransientError):
            await r.rerank("q", [_make_candidate()], top_k=1)

    async def test_permanent_error_propagates(self) -> None:
        client = MagicMock()
        client.rerank = AsyncMock(side_effect=LLMPermanentError("401"))
        client.close = AsyncMock()
        r = CohereReranker(api_key="k", _client=client)
        with pytest.raises(LLMPermanentError):
            await r.rerank("q", [_make_candidate()], top_k=1)

    async def test_unknown_exception_mapped_to_transient(self) -> None:
        client = MagicMock()
        client.rerank = AsyncMock(side_effect=RuntimeError("connection reset"))
        client.close = AsyncMock()
        r = CohereReranker(api_key="k", _client=client)
        with pytest.raises(LLMTransientError):
            await r.rerank("q", [_make_candidate()], top_k=1)
