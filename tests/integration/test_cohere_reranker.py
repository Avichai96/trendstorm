"""Integration test: CohereReranker against the live Cohere API.

Requires LLM__COHERE_API_KEY to be set in the environment or .env.local.
Skipped automatically if the key is absent or empty.

Run manually:
    uv run pytest tests/integration/test_cohere_reranker.py -m integration -s
"""
from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from trendstorm.domain.retrieval.models import RetrievedChunk
from trendstorm.infrastructure.retrieval.cohere_reranker import CohereReranker
from trendstorm.shared.config import get_settings
from trendstorm.shared.ids import new_id

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()


@pytest.fixture
async def reranker() -> AsyncGenerator[CohereReranker, None]:
    settings = get_settings()
    api_key = settings.llm.cohere_api_key.get_secret_value()
    if not api_key:
        pytest.skip("LLM__COHERE_API_KEY not set")

    r = CohereReranker(api_key=api_key, model=settings.llm.cohere_rerank_model)
    await r.connect()
    assert await r.health_check(), "Cohere client not initialised after connect()"
    yield r
    await r.close()


def _make_candidate(text: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=new_id(),
        score=0.5,
        text=text,
        document_id=new_id(),
        source_id=new_id(),
    )


class TestCohereRerankerIntegration:
    async def test_reranks_and_returns_top_k(self, reranker: CohereReranker) -> None:
        candidates = [
            _make_candidate("Climate change is causing sea level rise globally."),
            _make_candidate("Large language models are trained on vast text corpora."),
            _make_candidate("Reinforcement learning from human feedback improves LLM safety."),
            _make_candidate("Solar panel efficiency has increased dramatically over a decade."),
            _make_candidate("Anthropic's Constitutional AI method reduces harmful outputs."),
        ]
        results = await reranker.rerank(
            "AI safety and alignment techniques",
            candidates,
            top_k=3,
        )
        assert len(results) == 3
        # AI-related chunks should score highest; climate/solar should rank lower.
        # The top result must be one of the AI/LLM candidates.
        ai_texts = {candidates[1].text, candidates[2].text, candidates[4].text}
        assert results[0].text in ai_texts

    async def test_scores_are_in_descending_order(self, reranker: CohereReranker) -> None:
        candidates = [_make_candidate(f"document number {i}") for i in range(5)]
        results = await reranker.rerank("relevant query text", candidates, top_k=5)
        for i in range(len(results) - 1):
            assert results[i].score >= results[i + 1].score

    async def test_scores_are_between_zero_and_one(self, reranker: CohereReranker) -> None:
        candidates = [_make_candidate("sample text for reranking")]
        results = await reranker.rerank("some query", candidates, top_k=1)
        assert 0.0 <= results[0].score <= 1.0

    async def test_provenance_fields_preserved(self, reranker: CohereReranker) -> None:
        candidate = _make_candidate("AI safety research is important for alignment.")
        candidate_doc_id = candidate.document_id
        candidate_src_id = candidate.source_id
        results = await reranker.rerank("AI safety", [candidate], top_k=1)
        assert results[0].document_id == candidate_doc_id
        assert results[0].source_id == candidate_src_id
        assert results[0].chunk_id == candidate.chunk_id

    async def test_health_check_true_when_connected(self, reranker: CohereReranker) -> None:
        assert await reranker.health_check() is True

    async def test_close_releases_client(self, reranker: CohereReranker) -> None:
        await reranker.close()
        assert await reranker.health_check() is False
