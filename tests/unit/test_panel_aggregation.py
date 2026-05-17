"""Unit tests for LLMPanel aggregation strategies and quorum enforcement.

All tests are pure: no I/O, no LLM calls. JudgeVote objects are constructed
directly; the panel aggregator is tested in isolation.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from trendstorm.domain.evaluation.judge import (
    JudgeVote,
    PanelAggregation,
    PanelInsufficientVotesError,
)
from trendstorm.services.evaluation.panel import LLMPanel

# ---------------------------------------------------------------------------
# Helpers — build fake panels and fake judges
# ---------------------------------------------------------------------------

def _vote(score: float, passed: bool, rationale: str = "ok") -> JudgeVote:
    return JudgeVote(judge_model="test-model", score=score, passed=passed, rationale=rationale)


class _FakeJudge:
    """Implements LLMJudge Protocol — returns a pre-configured vote."""

    def __init__(self, vote: JudgeVote) -> None:
        self._vote = vote

    @property
    def model_id(self) -> str:
        return self._vote.judge_model

    async def judge(self, *, claim: str, evidence: str, context: dict) -> JudgeVote:
        return self._vote


# ---------------------------------------------------------------------------
# PanelInsufficientVotesError
# ---------------------------------------------------------------------------

class TestPanelInsufficientVotesError:
    def test_message(self):
        err = PanelInsufficientVotesError(received=1, required=2)
        assert "1" in str(err)
        assert "2" in str(err)

    def test_attributes(self):
        err = PanelInsufficientVotesError(received=0, required=3)
        assert err.received == 0
        assert err.required == 3


# ---------------------------------------------------------------------------
# JudgeVote validation
# ---------------------------------------------------------------------------

class TestJudgeVote:
    def test_score_clamped_low(self):
        with pytest.raises(ValidationError):
            JudgeVote(model_id="m", score=-0.1, passed=False, rationale="x")

    def test_score_clamped_high(self):
        with pytest.raises(ValidationError):
            JudgeVote(model_id="m", score=1.01, passed=True, rationale="x")

    def test_boundary_values(self):
        v0 = JudgeVote(judge_model="m", score=0.0, passed=False, rationale="x")
        v1 = JudgeVote(judge_model="m", score=1.0, passed=True, rationale="x")
        assert v0.score == 0.0
        assert v1.score == 1.0


# ---------------------------------------------------------------------------
# MEAN aggregation
# ---------------------------------------------------------------------------

class TestMeanAggregation:
    def _aggregate(self, votes: list[JudgeVote]) -> tuple[float, bool]:
        from trendstorm.services.evaluation.panel import _aggregate_votes
        result = _aggregate_votes(votes, PanelAggregation.MEAN)
        return result.score, result.passed

    def test_single_vote(self):
        score, passed = self._aggregate([_vote(0.8, True)])
        assert score == pytest.approx(0.8)
        assert passed is True

    def test_mean_of_three(self):
        votes = [_vote(0.6, False), _vote(0.8, True), _vote(1.0, True)]
        score, _ = self._aggregate(votes)
        assert score == pytest.approx(0.8)

    def test_passed_majority_passes(self):
        votes = [_vote(0.7, True), _vote(0.8, True), _vote(0.5, False)]
        _, passed = self._aggregate(votes)
        assert passed is True

    def test_passed_minority_fails(self):
        votes = [_vote(0.7, True), _vote(0.3, False), _vote(0.4, False)]
        _, passed = self._aggregate(votes)
        assert passed is False

    def test_tie_passed_false(self):
        # Tie (1 pass, 1 fail) → passed=False (conservative)
        votes = [_vote(0.9, True), _vote(0.2, False)]
        _, passed = self._aggregate(votes)
        assert passed is False


# ---------------------------------------------------------------------------
# MEDIAN aggregation
# ---------------------------------------------------------------------------

class TestMedianAggregation:
    def _aggregate(self, votes: list[JudgeVote]) -> tuple[float, bool]:
        from trendstorm.services.evaluation.panel import _aggregate_votes
        result = _aggregate_votes(votes, PanelAggregation.MEDIAN)
        return result.score, result.passed

    def test_odd_count(self):
        votes = [_vote(0.2, False), _vote(0.7, True), _vote(0.9, True)]
        score, _ = self._aggregate(votes)
        assert score == pytest.approx(0.7)

    def test_even_count(self):
        votes = [_vote(0.4, False), _vote(0.6, True), _vote(0.8, True), _vote(1.0, True)]
        score, _ = self._aggregate(votes)
        # median of [0.4, 0.6, 0.8, 1.0] = (0.6 + 0.8) / 2 = 0.7
        assert score == pytest.approx(0.7)

    def test_single_vote(self):
        score, passed = self._aggregate([_vote(0.55, True)])
        assert score == pytest.approx(0.55)
        assert passed is True


# ---------------------------------------------------------------------------
# MIN aggregation
# ---------------------------------------------------------------------------

class TestMinAggregation:
    def _aggregate(self, votes: list[JudgeVote]) -> tuple[float, bool]:
        from trendstorm.services.evaluation.panel import _aggregate_votes
        result = _aggregate_votes(votes, PanelAggregation.MIN)
        return result.score, result.passed

    def test_returns_minimum(self):
        votes = [_vote(0.9, True), _vote(0.1, False), _vote(0.7, True)]
        score, _ = self._aggregate(votes)
        assert score == pytest.approx(0.1)

    def test_passed_follows_min(self):
        votes = [_vote(0.9, True), _vote(0.1, False)]
        _, passed = self._aggregate(votes)
        assert passed is False


# ---------------------------------------------------------------------------
# MAJORITY aggregation
# ---------------------------------------------------------------------------

class TestMajorityAggregation:
    def _aggregate(self, votes: list[JudgeVote]) -> tuple[float, bool]:
        from trendstorm.services.evaluation.panel import _aggregate_votes
        result = _aggregate_votes(votes, PanelAggregation.MAJORITY)
        return result.score, result.passed

    def test_majority_true(self):
        votes = [_vote(0.8, True), _vote(0.7, True), _vote(0.4, False)]
        _, passed = self._aggregate(votes)
        assert passed is True

    def test_majority_false(self):
        votes = [_vote(0.8, True), _vote(0.3, False), _vote(0.2, False)]
        _, passed = self._aggregate(votes)
        assert passed is False

    def test_score_is_mean_of_majority_votes(self):
        votes = [_vote(0.8, True), _vote(0.6, True), _vote(0.2, False)]
        score, _ = self._aggregate(votes)
        # mean of (0.8, 0.6, 0.2) = 0.533...
        assert score == pytest.approx((0.8 + 0.6 + 0.2) / 3, rel=1e-3)


# ---------------------------------------------------------------------------
# Min quorum enforcement in LLMPanel
# ---------------------------------------------------------------------------

class _FailingJudge:
    """Implements LLMJudge Protocol — always raises."""

    @property
    def model_id(self) -> str:
        return "failing"

    async def judge(self, *, claim: str, evidence: str, context: dict) -> JudgeVote:
        raise RuntimeError("simulated judge failure")


class TestMinQuorum:
    @pytest.mark.asyncio
    async def test_quorum_met_returns_result(self):

        judge = _FakeJudge(_vote(0.8, True))
        panel = LLMPanel(judges=[judge], min_quorum=1)

        result = await panel.vote(
            claim="Some claim",
            evidence="Some evidence",
            context={"system_prompt": "judge", "tool_schema": {}, "tool_name": "record_vote"},
        )
        assert result.score == pytest.approx(0.8)

    @pytest.mark.asyncio
    async def test_quorum_not_met_raises(self):

        panel = LLMPanel(judges=[_FailingJudge(), _FailingJudge()], min_quorum=2)

        with pytest.raises(PanelInsufficientVotesError) as exc_info:
            await panel.vote(
                claim="claim",
                evidence="evidence",
                context={"system_prompt": "j", "tool_schema": {}, "tool_name": "v"},
            )
        assert exc_info.value.received == 0
        assert exc_info.value.required == 2

    @pytest.mark.asyncio
    async def test_partial_failure_still_meets_quorum(self):

        good_judge = _FakeJudge(_vote(0.75, True))
        panel = LLMPanel(judges=[_FailingJudge(), good_judge], min_quorum=1)

        result = await panel.vote(
            claim="claim",
            evidence="evidence",
            context={"system_prompt": "j", "tool_schema": {}, "tool_name": "v"},
        )
        assert result.score == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_votes_raises(self):
        from trendstorm.services.evaluation.panel import _aggregate_votes
        with pytest.raises(PanelInsufficientVotesError):
            _aggregate_votes([], PanelAggregation.MEAN)

    def test_all_zeros(self):
        from trendstorm.services.evaluation.panel import _aggregate_votes
        votes = [_vote(0.0, False), _vote(0.0, False)]
        result = _aggregate_votes(votes, PanelAggregation.MEAN)
        assert result.score == 0.0
        assert result.passed is False

    def test_all_ones(self):
        from trendstorm.services.evaluation.panel import _aggregate_votes
        votes = [_vote(1.0, True), _vote(1.0, True), _vote(1.0, True)]
        result = _aggregate_votes(votes, PanelAggregation.MEAN)
        assert result.score == pytest.approx(1.0)
        assert result.passed is True
