"""Unit tests for the Stage state machine."""
from __future__ import annotations

import itertools

import pytest

from trendstorm.agents.stages import Stage, allowed_next_stages, is_valid_transition


@pytest.mark.unit
class TestStageTransitions:
    """Verify the state machine is correctly specified.

    The state machine is a contract — every stage must list its allowed
    next stages. Tests pin down the rules so we catch accidental edits.
    """

    def test_terminal_stages_have_no_successors(self) -> None:
        for terminal in (Stage.COMPLETED, Stage.FAILED, Stage.CANCELLED, Stage.REJECTED):
            assert allowed_next_stages(terminal) == frozenset()
            assert terminal.is_terminal is True

    def test_pending_progresses_to_ingesting(self) -> None:
        assert is_valid_transition(Stage.PENDING, Stage.INGESTING)

    def test_pending_cannot_jump_to_publishing(self) -> None:
        assert not is_valid_transition(Stage.PENDING, Stage.PUBLISHING)

    def test_any_stage_can_fail(self) -> None:
        # AWAITING_REVIEW does not go to FAILED; it goes to REJECTED (distinct terminal state).
        for s in (Stage.PENDING, Stage.INGESTING, Stage.EMBEDDING,
                  Stage.RETRIEVING, Stage.ANALYZING, Stage.PUBLISHING):
            assert is_valid_transition(s, Stage.FAILED), f"{s} -> FAILED disallowed"

    def test_awaiting_review_cannot_fail_only_reject(self) -> None:
        """AWAITING_REVIEW skips FAILED — reviewer decisions produce REJECTED, not FAILED."""
        assert not is_valid_transition(Stage.AWAITING_REVIEW, Stage.FAILED)
        assert is_valid_transition(Stage.AWAITING_REVIEW, Stage.REJECTED)

    def test_any_stage_can_be_cancelled(self) -> None:
        for s in (Stage.PENDING, Stage.INGESTING, Stage.EMBEDDING,
                  Stage.RETRIEVING, Stage.ANALYZING, Stage.AWAITING_REVIEW,
                  Stage.PUBLISHING, Stage.MEMORY_CONSOLIDATION):
            assert is_valid_transition(s, Stage.CANCELLED), f"{s} -> CANCELLED disallowed"

    def test_self_retry_allowed_for_work_stages(self) -> None:
        """Work stages can stay in place (retry semantics)."""
        for s in (Stage.INGESTING, Stage.EMBEDDING, Stage.RETRIEVING,
                  Stage.ANALYZING, Stage.PUBLISHING):
            assert is_valid_transition(s, s), f"{s} -> {s} self-loop disallowed"

    def test_analyzing_can_loop_to_retrieving(self) -> None:
        """The refinement loop is the only backward transition allowed."""
        assert is_valid_transition(Stage.ANALYZING, Stage.RETRIEVING)

    def test_no_other_backward_transitions(self) -> None:
        """Sanity: don't accidentally allow going backwards from EMBEDDING -> INGESTING."""
        assert not is_valid_transition(Stage.EMBEDDING, Stage.INGESTING)
        assert not is_valid_transition(Stage.RETRIEVING, Stage.EMBEDDING)
        assert not is_valid_transition(Stage.PUBLISHING, Stage.ANALYZING)

    def test_completed_is_only_reachable_from_memory_consolidation(self) -> None:
        """Phase 15.5: MEMORY_CONSOLIDATION is the gate before COMPLETED."""
        for s in Stage:
            if s == Stage.MEMORY_CONSOLIDATION:
                assert is_valid_transition(s, Stage.COMPLETED)
            else:
                assert not is_valid_transition(s, Stage.COMPLETED), (
                    f"{s} -> COMPLETED should not be allowed"
                )

    def test_publishing_leads_to_memory_consolidation(self) -> None:
        assert is_valid_transition(Stage.PUBLISHING, Stage.MEMORY_CONSOLIDATION)
        assert not is_valid_transition(Stage.PUBLISHING, Stage.COMPLETED)

    def test_happy_path_walk(self) -> None:
        """Sanity check the entire forward pipeline is reachable (Phase 15.5 path)."""
        chain = [
            Stage.PENDING, Stage.INGESTING, Stage.EMBEDDING,
            Stage.RETRIEVING, Stage.ANALYZING, Stage.PUBLISHING,
            Stage.MEMORY_CONSOLIDATION, Stage.COMPLETED,
        ]
        for a, b in itertools.pairwise(chain):
            assert is_valid_transition(a, b), f"Happy-path step {a} -> {b} disallowed"

    def test_hitl_transitions(self) -> None:
        """ANALYZING → AWAITING_REVIEW → PUBLISHING/ANALYZING/REJECTED are valid."""
        assert is_valid_transition(Stage.ANALYZING, Stage.AWAITING_REVIEW)
        assert is_valid_transition(Stage.AWAITING_REVIEW, Stage.PUBLISHING)    # approve
        assert is_valid_transition(Stage.AWAITING_REVIEW, Stage.ANALYZING)     # request_refinement
        assert is_valid_transition(Stage.AWAITING_REVIEW, Stage.REJECTED)      # reject

    def test_rejected_is_terminal(self) -> None:
        assert Stage.REJECTED.is_terminal is True
        assert allowed_next_stages(Stage.REJECTED) == frozenset()

    def test_rejected_not_reachable_from_non_review_stages(self) -> None:
        """REJECTED is only reachable from AWAITING_REVIEW, not from other stages."""
        for s in (Stage.PENDING, Stage.INGESTING, Stage.EMBEDDING,
                  Stage.RETRIEVING, Stage.ANALYZING, Stage.PUBLISHING):
            assert not is_valid_transition(s, Stage.REJECTED), (
                f"{s} -> REJECTED should not be allowed (only from AWAITING_REVIEW)"
            )
