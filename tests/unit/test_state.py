"""Unit tests for JobState helpers."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from trendstorm.agents.stages import Stage
from trendstorm.agents.state import (
    DEFAULT_RETRY_BUDGETS,
    MAX_REFINEMENT_LOOPS,
    JobState,
    ObservabilityContext,
    SourceRef,
)
from trendstorm.shared.ids import new_id


def _make_state(**overrides) -> JobState:
    """Helper to build a JobState with minimal boilerplate."""
    defaults = {
        "job_id": new_id(),
        "tenant_id": new_id(),
        "category_id": new_id(),
        "observability": ObservabilityContext(correlation_id=new_id()),
    }
    return JobState(**{**defaults, **overrides})


@pytest.mark.unit
class TestRetryBudget:
    def test_full_budget_initially(self) -> None:
        state = _make_state()
        assert state.remaining_budget(Stage.INGESTING) == DEFAULT_RETRY_BUDGETS[Stage.INGESTING]
        assert state.has_budget(Stage.INGESTING)

    def test_attempts_deplete_budget(self) -> None:
        state = _make_state(attempts={Stage.INGESTING: 3})
        budget = DEFAULT_RETRY_BUDGETS[Stage.INGESTING]
        assert state.remaining_budget(Stage.INGESTING) == budget - 3

    def test_budget_clamps_at_zero(self) -> None:
        """Even with 100 attempts, remaining never goes negative."""
        state = _make_state(attempts={Stage.INGESTING: 100})
        assert state.remaining_budget(Stage.INGESTING) == 0
        assert not state.has_budget(Stage.INGESTING)

    def test_per_stage_independence(self) -> None:
        state = _make_state(attempts={Stage.INGESTING: 10})
        # Other stages still have their full budget.
        assert state.has_budget(Stage.EMBEDDING)
        assert state.has_budget(Stage.ANALYZING)


@pytest.mark.unit
class TestRefinementLoops:
    def test_can_refine_initially(self) -> None:
        state = _make_state()
        assert state.can_refine() is True

    def test_can_refine_at_boundary(self) -> None:
        state = _make_state(refinement_loops=MAX_REFINEMENT_LOOPS - 1)
        assert state.can_refine() is True

    def test_cannot_refine_at_cap(self) -> None:
        state = _make_state(refinement_loops=MAX_REFINEMENT_LOOPS)
        assert state.can_refine() is False


@pytest.mark.unit
class TestInitialFactory:
    def test_initial_state_well_formed(self) -> None:
        state = JobState.initial(
            tenant_id="t1" + "x" * 24,
            category_id="c1" + "x" * 24,
            sources=[SourceRef(id="s1", type="http", label="example.com")],
            correlation_id="cid",
        )
        assert state.stage == Stage.PENDING
        assert state.refinement_loops == 0
        assert state.attempts == {}
        assert len(state.sources) == 1
        assert state.observability.correlation_id == "cid"

    def test_initial_generates_job_id(self) -> None:
        a = JobState.initial(
            tenant_id="t", category_id="c", sources=[], correlation_id="x",
        )
        b = JobState.initial(
            tenant_id="t", category_id="c", sources=[], correlation_id="x",
        )
        assert a.job_id != b.job_id
        assert len(a.job_id) == 26


@pytest.mark.unit
class TestSerialization:
    """State must round-trip via Pydantic for LangGraph checkpointing."""

    def test_dump_and_load_preserves_state(self) -> None:
        original = _make_state(
            attempts={Stage.INGESTING: 2, Stage.EMBEDDING: 1},
            refinement_loops=1,
        )
        dumped = original.model_dump(mode="json")
        restored = JobState.model_validate(dumped)
        assert restored.attempts == original.attempts
        assert restored.refinement_loops == original.refinement_loops
        assert restored.stage == original.stage

    def test_extra_fields_forbidden(self) -> None:
        """Schema is closed — unknown fields are a programming bug."""
        with pytest.raises(ValidationError):
            _make_state(unknown_field="garbage")
