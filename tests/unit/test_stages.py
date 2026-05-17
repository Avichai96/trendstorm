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
        for terminal in (Stage.COMPLETED, Stage.FAILED, Stage.CANCELLED):
            assert allowed_next_stages(terminal) == frozenset()
            assert terminal.is_terminal is True

    def test_pending_progresses_to_ingesting(self) -> None:
        assert is_valid_transition(Stage.PENDING, Stage.INGESTING)

    def test_pending_cannot_jump_to_publishing(self) -> None:
        assert not is_valid_transition(Stage.PENDING, Stage.PUBLISHING)

    def test_any_stage_can_fail(self) -> None:
        for s in (Stage.PENDING, Stage.INGESTING, Stage.EMBEDDING,
                  Stage.RETRIEVING, Stage.ANALYZING, Stage.PUBLISHING):
            assert is_valid_transition(s, Stage.FAILED), f"{s} -> FAILED disallowed"

    def test_any_stage_can_be_cancelled(self) -> None:
        for s in (Stage.PENDING, Stage.INGESTING, Stage.EMBEDDING,
                  Stage.RETRIEVING, Stage.ANALYZING, Stage.PUBLISHING):
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

    def test_completed_is_only_reachable_from_publishing(self) -> None:
        for s in Stage:
            if s == Stage.PUBLISHING:
                assert is_valid_transition(s, Stage.COMPLETED)
            else:
                assert not is_valid_transition(s, Stage.COMPLETED), (
                    f"{s} -> COMPLETED should not be allowed"
                )

    def test_happy_path_walk(self) -> None:
        """Sanity check the entire forward pipeline is reachable."""
        chain = [
            Stage.PENDING, Stage.INGESTING, Stage.EMBEDDING,
            Stage.RETRIEVING, Stage.ANALYZING, Stage.PUBLISHING,
            Stage.COMPLETED,
        ]
        for a, b in itertools.pairwise(chain):
            assert is_valid_transition(a, b), f"Happy-path step {a} -> {b} disallowed"
