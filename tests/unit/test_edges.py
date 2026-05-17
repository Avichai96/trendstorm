"""Unit tests for the conditional edge routing functions.

These tests stub state and verify the routing decision tree. No graph,
no I/O, no agents — just pure functions.
"""
from __future__ import annotations

import pytest

from trendstorm.agents.orchestrator.edges import (
    NODE_ANALYZE,
    NODE_EMBED,
    NODE_END,
    NODE_FAIL,
    NODE_INGEST,
    NODE_PUBLISH,
    NODE_REFINE,
    NODE_RETRIEVE,
    after_analyze,
    after_embed,
    after_ingest,
    after_publish,
    after_retrieve,
)
from trendstorm.agents.stages import Stage
from trendstorm.agents.state import (
    MAX_REFINEMENT_LOOPS,
    AnalysisState,
    ChunkRef,
    DocumentRef,
    IngestionState,
    JobState,
    KnowledgeState,
    ObservabilityContext,
    PublishingState,
    RetrievalState,
    SourceRef,
)
from trendstorm.shared.ids import new_id


def _state(**overrides) -> JobState:
    defaults = {
        "job_id": new_id(),
        "tenant_id": new_id(),
        "category_id": new_id(),
        "sources": [SourceRef(id="s1", type="http", label="ex")],
        "observability": ObservabilityContext(correlation_id="cid"),
    }
    return JobState(**{**defaults, **overrides})


# ---------------------------------------------------------------------------
# after_ingest
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestAfterIngest:
    def test_documents_present_goes_to_embed(self) -> None:
        s = _state(
            stage=Stage.INGESTING,
            ingestion=IngestionState(raw_documents=[
                DocumentRef(id="d1", source_id="s1", content_hash="h"),
            ]),
        )
        assert after_ingest(s) == NODE_EMBED

    def test_empty_with_budget_retries(self) -> None:
        s = _state(stage=Stage.INGESTING)  # default budget = 5, attempts = {}
        assert after_ingest(s) == NODE_INGEST

    def test_empty_no_budget_fails(self) -> None:
        s = _state(
            stage=Stage.INGESTING,
            attempts={Stage.INGESTING: 99},  # exhausted
        )
        assert after_ingest(s) == NODE_FAIL


# ---------------------------------------------------------------------------
# after_embed
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestAfterEmbed:
    def test_chunks_present_goes_to_retrieve(self) -> None:
        s = _state(
            stage=Stage.EMBEDDING,
            knowledge=KnowledgeState(chunk_refs=[ChunkRef(id="c1", document_id="d1")]),
        )
        assert after_embed(s) == NODE_RETRIEVE

    def test_empty_with_budget_retries(self) -> None:
        s = _state(stage=Stage.EMBEDDING)
        assert after_embed(s) == NODE_EMBED

    def test_empty_no_budget_fails(self) -> None:
        s = _state(stage=Stage.EMBEDDING, attempts={Stage.EMBEDDING: 99})
        assert after_embed(s) == NODE_FAIL


# ---------------------------------------------------------------------------
# after_retrieve
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestAfterRetrieve:
    def test_retrieved_chunks_goes_to_analyze(self) -> None:
        s = _state(
            stage=Stage.RETRIEVING,
            retrieval=RetrievalState(retrieved_chunk_ids=["c1", "c2"]),
        )
        assert after_retrieve(s) == NODE_ANALYZE

    def test_empty_with_budget_retries(self) -> None:
        s = _state(stage=Stage.RETRIEVING)
        assert after_retrieve(s) == NODE_RETRIEVE

    def test_empty_no_budget_fails(self) -> None:
        s = _state(stage=Stage.RETRIEVING, attempts={Stage.RETRIEVING: 99})
        assert after_retrieve(s) == NODE_FAIL


# ---------------------------------------------------------------------------
# after_analyze (the most interesting edge)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestAfterAnalyze:
    def test_passed_goes_to_publish(self) -> None:
        s = _state(
            stage=Stage.ANALYZING,
            analysis=AnalysisState(validation_passed=True, validation_score=0.9),
        )
        assert after_analyze(s) == NODE_PUBLISH

    def test_failed_with_refinement_budget_refines(self) -> None:
        s = _state(
            stage=Stage.ANALYZING,
            analysis=AnalysisState(validation_passed=False, validation_score=0.5),
            refinement_loops=0,
        )
        assert after_analyze(s) == NODE_REFINE

    def test_failed_at_refinement_cap_publishes_anyway(self) -> None:
        """Graceful degradation: low confidence still beats no report."""
        s = _state(
            stage=Stage.ANALYZING,
            analysis=AnalysisState(validation_passed=False, validation_score=0.5),
            refinement_loops=MAX_REFINEMENT_LOOPS,
        )
        assert after_analyze(s) == NODE_PUBLISH

    def test_failed_no_analyze_budget_publishes_anyway(self) -> None:
        s = _state(
            stage=Stage.ANALYZING,
            analysis=AnalysisState(validation_passed=False, validation_score=0.5),
            attempts={Stage.ANALYZING: 99},
        )
        assert after_analyze(s) == NODE_PUBLISH


# ---------------------------------------------------------------------------
# after_publish
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestAfterPublish:
    def test_report_id_goes_to_end(self) -> None:
        s = _state(
            stage=Stage.PUBLISHING,
            publishing=PublishingState(report_doc_id="r1"),
        )
        assert after_publish(s) == NODE_END

    def test_empty_with_budget_retries(self) -> None:
        s = _state(stage=Stage.PUBLISHING)
        assert after_publish(s) == NODE_PUBLISH

    def test_empty_no_budget_fails(self) -> None:
        s = _state(stage=Stage.PUBLISHING, attempts={Stage.PUBLISHING: 99})
        assert after_publish(s) == NODE_FAIL
