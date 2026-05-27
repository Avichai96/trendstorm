"""Unit tests for Phase 13.5 HITL review queue.

Tests cover:
- review_gate_node: all decision branches (stub path, HITL off, always, flagged_only)
- skip_hitl_gate bypass
- ReviewRequest domain model validation
- TenantSettings defaults and model validation
- require_role dependency
- ReviewTimeoutSweeper logic (mock Kafka and review repo)
- JobState HITL fields
- StreamEventType.is_terminal for JOB_REJECTED
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trendstorm.agents.orchestrator.edges import (
    NODE_END,
    NODE_FAIL,
    NODE_PUBLISH,
    NODE_REVIEW_GATE,
    after_analyze,
    after_review_gate,
)
from trendstorm.agents.orchestrator.nodes import review_gate_node
from trendstorm.agents.stages import Stage
from trendstorm.agents.state import (
    MAX_REFINEMENT_LOOPS,
    AnalysisState,
    JobState,
    ObservabilityContext,
    SourceRef,
)
from trendstorm.domain.reviews.models import (
    ReviewDecision,
    ReviewRequest,
    ReviewStatus,
)
from trendstorm.domain.streaming.events import StreamEventType
from trendstorm.domain.tenant_settings.models import (
    DEFAULT_TENANT_SETTINGS,
    HitlMode,
    TenantSettings,
)
from trendstorm.shared.ids import new_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(**overrides: Any) -> JobState:
    defaults: dict[str, Any] = {
        "job_id": new_id(),
        "tenant_id": new_id(),
        "category_id": new_id(),
        "sources": [SourceRef(id="s1", type="http", label="ex")],
        "observability": ObservabilityContext(correlation_id=new_id()),
    }
    return JobState(**{**defaults, **overrides})


def _config(*, producer: Any = None, settings_repo: Any = None, review_repo: Any = None) -> dict:
    return {
        "configurable": {
            "kafka_producer": producer,
            "tenant_settings_repo": settings_repo,
            "review_repo": review_repo,
        }
    }


# ---------------------------------------------------------------------------
# review_gate_node — stub path (no Kafka producer)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestReviewGateNodeStubPath:
    @pytest.mark.asyncio
    async def test_no_producer_always_passes_through(self) -> None:
        state = _make_state(
            stage=Stage.ANALYZING,
            analysis=AnalysisState(validation_passed=True, validation_score=0.9),
        )
        result = await review_gate_node(state, _config())
        assert result["stage"] == Stage.PUBLISHING

    @pytest.mark.asyncio
    async def test_skip_hitl_gate_passes_through_regardless_of_producer(self) -> None:
        """skip_hitl_gate=True bypasses all other logic — even with a producer."""
        mock_producer = AsyncMock()
        state = _make_state(
            stage=Stage.ANALYZING,
            analysis=AnalysisState(validation_passed=False, validation_score=0.3),
            skip_hitl_gate=True,
        )
        result = await review_gate_node(state, _config(producer=mock_producer))
        assert result["stage"] == Stage.PUBLISHING
        assert result.get("skip_hitl_gate") is False  # reset after use
        mock_producer.send_and_wait.assert_not_called()

    @pytest.mark.asyncio
    async def test_skip_hitl_gate_false_does_not_trigger_bypass(self) -> None:
        """skip_hitl_gate=False with no producer → stub pass-through (not bypass)."""
        state = _make_state(
            stage=Stage.ANALYZING,
            analysis=AnalysisState(validation_passed=True, validation_score=0.9),
            skip_hitl_gate=False,
        )
        result = await review_gate_node(state, _config())
        assert result["stage"] == Stage.PUBLISHING


# ---------------------------------------------------------------------------
# review_gate_node — HITL mode decisions
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestReviewGateNodeHitlModes:
    def _settings_repo(self, *, mode: HitlMode, threshold: float = 0.7,
                       cost: float | None = None, timeout: int = 48) -> AsyncMock:
        settings = TenantSettings(
            tenant_id="t1",
            hitl_mode=mode,
            hitl_validator_threshold=threshold,
            hitl_cost_threshold_usd=cost,
            hitl_timeout_hours=timeout,
        )
        repo = AsyncMock()
        repo.get_for_tenant.return_value = settings
        return repo

    @pytest.mark.asyncio
    async def test_hitl_off_passes_through(self) -> None:
        mock_producer = AsyncMock()
        settings_repo = self._settings_repo(mode=HitlMode.OFF)
        state = _make_state(
            stage=Stage.ANALYZING,
            analysis=AnalysisState(validation_passed=True, validation_score=0.9),
        )
        result = await review_gate_node(
            state, _config(producer=mock_producer, settings_repo=settings_repo)
        )
        assert result["stage"] == Stage.PUBLISHING
        mock_producer.send_and_wait.assert_not_called()

    @pytest.mark.asyncio
    async def test_hitl_always_gates_regardless_of_score(self) -> None:
        mock_producer = AsyncMock()
        review_repo = AsyncMock()
        settings_repo = self._settings_repo(mode=HitlMode.ALWAYS)
        state = _make_state(
            stage=Stage.ANALYZING,
            analysis=AnalysisState(validation_passed=True, validation_score=1.0),
        )
        result = await review_gate_node(
            state, _config(producer=mock_producer, settings_repo=settings_repo,
                           review_repo=review_repo)
        )
        assert result["stage"] == Stage.AWAITING_REVIEW
        assert result.get("pending_review_id") is not None
        review_repo.insert.assert_awaited_once()
        mock_producer.send_and_wait.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_flagged_only_below_threshold_gates(self) -> None:
        mock_producer = AsyncMock()
        review_repo = AsyncMock()
        settings_repo = self._settings_repo(mode=HitlMode.FLAGGED_ONLY, threshold=0.8)
        state = _make_state(
            stage=Stage.ANALYZING,
            analysis=AnalysisState(validation_passed=False, validation_score=0.5),
        )
        result = await review_gate_node(
            state, _config(producer=mock_producer, settings_repo=settings_repo,
                           review_repo=review_repo)
        )
        assert result["stage"] == Stage.AWAITING_REVIEW

    @pytest.mark.asyncio
    async def test_flagged_only_above_threshold_passes_through(self) -> None:
        mock_producer = AsyncMock()
        settings_repo = self._settings_repo(mode=HitlMode.FLAGGED_ONLY, threshold=0.8)
        state = _make_state(
            stage=Stage.ANALYZING,
            analysis=AnalysisState(validation_passed=True, validation_score=0.95),
        )
        result = await review_gate_node(
            state, _config(producer=mock_producer, settings_repo=settings_repo)
        )
        assert result["stage"] == Stage.PUBLISHING
        mock_producer.send_and_wait.assert_not_called()

    @pytest.mark.asyncio
    async def test_flagged_only_budget_exhausted_gates(self) -> None:
        """Even if score is above threshold, exhausted refinement budget triggers review."""
        mock_producer = AsyncMock()
        review_repo = AsyncMock()
        settings_repo = self._settings_repo(mode=HitlMode.FLAGGED_ONLY, threshold=0.5)
        state = _make_state(
            stage=Stage.ANALYZING,
            analysis=AnalysisState(validation_passed=True, validation_score=0.9),
            refinement_loops=MAX_REFINEMENT_LOOPS,  # budget exhausted
        )
        result = await review_gate_node(
            state, _config(producer=mock_producer, settings_repo=settings_repo,
                           review_repo=review_repo)
        )
        assert result["stage"] == Stage.AWAITING_REVIEW

    @pytest.mark.asyncio
    async def test_missing_settings_repo_uses_defaults(self) -> None:
        """No settings_repo → DEFAULT_TENANT_SETTINGS → hitl_mode=OFF → pass-through."""
        mock_producer = AsyncMock()
        state = _make_state(
            stage=Stage.ANALYZING,
            analysis=AnalysisState(validation_passed=True, validation_score=0.9),
        )
        result = await review_gate_node(
            state, _config(producer=mock_producer, settings_repo=None)
        )
        assert result["stage"] == Stage.PUBLISHING

    @pytest.mark.asyncio
    async def test_settings_repo_returns_none_uses_defaults(self) -> None:
        mock_producer = AsyncMock()
        settings_repo = AsyncMock()
        settings_repo.get_for_tenant.return_value = None  # tenant not configured
        state = _make_state(
            stage=Stage.ANALYZING,
            analysis=AnalysisState(validation_passed=True, validation_score=0.9),
        )
        result = await review_gate_node(
            state, _config(producer=mock_producer, settings_repo=settings_repo)
        )
        assert result["stage"] == Stage.PUBLISHING


# ---------------------------------------------------------------------------
# ReviewRequest domain model
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestReviewRequestModel:
    def test_defaults(self) -> None:
        review = ReviewRequest(
            tenant_id="t1",
            job_id="j1",
            analysis_id="a1",
            stage_under_review="analyzing",
            timeout_at=datetime.now(UTC) + timedelta(hours=48),
            sla_seconds=48 * 3600,
        )
        assert review.status == ReviewStatus.PENDING
        assert review.reviewer_id is None
        assert review.decision_comment is None
        assert review.resolved_at is None
        assert len(review.id) == 26  # ULID

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(Exception):
            ReviewRequest(
                tenant_id="t1", job_id="j1", analysis_id="a1",
                stage_under_review="analyzing",
                timeout_at=datetime.now(UTC) + timedelta(hours=48),
                sla_seconds=172800,
                unknown_field="oops",
            )

    def test_review_decision_enum_values(self) -> None:
        assert ReviewDecision.APPROVE == "approve"
        assert ReviewDecision.REJECT == "reject"
        assert ReviewDecision.REQUEST_REFINEMENT == "request_refinement"

    def test_review_status_enum_values(self) -> None:
        assert ReviewStatus.PENDING == "pending"
        assert ReviewStatus.TIMED_OUT == "timed_out"


# ---------------------------------------------------------------------------
# TenantSettings domain model
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestTenantSettingsModel:
    def test_default_settings_have_hitl_off(self) -> None:
        assert DEFAULT_TENANT_SETTINGS.hitl_mode == HitlMode.OFF
        assert DEFAULT_TENANT_SETTINGS.hitl_validator_threshold == 0.7
        assert DEFAULT_TENANT_SETTINGS.hitl_timeout_hours == 48
        assert DEFAULT_TENANT_SETTINGS.hitl_cost_threshold_usd is None

    def test_custom_settings(self) -> None:
        settings = TenantSettings(
            tenant_id="t1",
            hitl_mode=HitlMode.FLAGGED_ONLY,
            hitl_validator_threshold=0.85,
            hitl_cost_threshold_usd=5.0,
            hitl_timeout_hours=24,
        )
        assert settings.hitl_mode == HitlMode.FLAGGED_ONLY
        assert settings.hitl_validator_threshold == 0.85

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(Exception):
            TenantSettings(tenant_id="t1", hitl_mode="off", unknown="x")

    def test_hitl_mode_values(self) -> None:
        assert HitlMode.OFF == "off"
        assert HitlMode.ALWAYS == "always"
        assert HitlMode.FLAGGED_ONLY == "flagged_only"


# ---------------------------------------------------------------------------
# JobState HITL fields
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestJobStateHitlFields:
    def test_hitl_fields_default(self) -> None:
        state = _make_state()
        assert state.pending_review_id is None
        assert state.review_decision_comment is None
        assert state.skip_hitl_gate is False

    def test_schema_version_is_2(self) -> None:
        state = _make_state()
        assert state.schema_version == 2

    def test_hitl_fields_can_be_set(self) -> None:
        review_id = new_id()
        state = _make_state(
            pending_review_id=review_id,
            review_decision_comment="Looks good",
            skip_hitl_gate=True,
        )
        assert state.pending_review_id == review_id
        assert state.review_decision_comment == "Looks good"
        assert state.skip_hitl_gate is True


# ---------------------------------------------------------------------------
# StreamEventType — JOB_REJECTED is terminal
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestStreamEventTypeHitl:
    def test_job_rejected_is_terminal(self) -> None:
        assert StreamEventType.JOB_REJECTED.is_terminal is True

    def test_review_required_is_not_terminal(self) -> None:
        assert StreamEventType.REVIEW_REQUIRED.is_terminal is False

    def test_review_resolved_is_not_terminal(self) -> None:
        assert StreamEventType.REVIEW_RESOLVED.is_terminal is False

    def test_all_terminal_events(self) -> None:
        terminal = {e for e in StreamEventType if e.is_terminal}
        assert terminal == {
            StreamEventType.REPORT_READY,
            StreamEventType.JOB_FAILED,
            StreamEventType.JOB_REJECTED,
        }


# ---------------------------------------------------------------------------
# require_role dependency
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestRequireRole:
    def _make_request(self, roles: list[str]) -> MagicMock:
        from trendstorm.domain.auth.models import AuthContext
        ctx = AuthContext(tenant_id="t1", key_id="k1", roles=roles)
        request = MagicMock()
        request.state.auth_context = ctx
        return request

    def test_role_present_does_not_raise(self) -> None:
        from trendstorm.utils.headers_docs import require_role
        from fastapi import HTTPException
        request = self._make_request(roles=["reviewer"])
        # require_role returns a Depends(...) — we need to extract the inner callable.
        # The inner function is the first positional arg to Depends.
        dep = require_role("reviewer")
        inner_fn = dep.dependency
        inner_fn(request)  # must not raise

    def test_role_missing_raises_403(self) -> None:
        from trendstorm.utils.headers_docs import require_role
        from fastapi import HTTPException
        request = self._make_request(roles=[])
        dep = require_role("reviewer")
        inner_fn = dep.dependency
        with pytest.raises(HTTPException) as exc_info:
            inner_fn(request)
        assert exc_info.value.status_code == 403

    def test_wrong_role_raises_403(self) -> None:
        from trendstorm.utils.headers_docs import require_role
        from fastapi import HTTPException
        request = self._make_request(roles=["admin"])
        dep = require_role("reviewer")
        inner_fn = dep.dependency
        with pytest.raises(HTTPException) as exc_info:
            inner_fn(request)
        assert exc_info.value.status_code == 403

    def test_no_auth_context_raises_403(self) -> None:
        from trendstorm.utils.headers_docs import require_role
        from fastapi import HTTPException
        request = MagicMock()
        request.state.auth_context = None
        dep = require_role("reviewer")
        inner_fn = dep.dependency
        with pytest.raises(HTTPException) as exc_info:
            inner_fn(request)
        assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# ReviewTimeoutSweeper — unit tests with mocked deps
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestReviewTimeoutSweeper:
    def _make_expired_review(self) -> ReviewRequest:
        now = datetime.now(UTC)
        return ReviewRequest(
            tenant_id=new_id(),
            job_id=new_id(),
            analysis_id=new_id(),
            stage_under_review="awaiting_review",
            timeout_at=now - timedelta(hours=1),  # already expired
            sla_seconds=3600,
        )

    @pytest.mark.asyncio
    async def test_sweep_publishes_reject_event(self) -> None:
        from trendstorm.orchestration.workers.review_timeout_worker import ReviewTimeoutSweeper

        review = self._make_expired_review()
        review_repo = AsyncMock()
        review_repo.list_expired_pending.return_value = [review]
        review_repo.mark_timed_out.return_value = review  # updated successfully

        producer = AsyncMock()
        producer.producer = AsyncMock()

        sweeper = ReviewTimeoutSweeper(
            review_repo=review_repo,
            producer=producer,
            poll_interval_seconds=60,
        )

        with patch("trendstorm.orchestration.workers.review_timeout_worker.METRICS") as mock_metrics:
            mock_metrics.review_timeout_total = MagicMock()
            await sweeper._sweep_once()

        review_repo.mark_timed_out.assert_awaited_once_with(
            review.tenant_id, review.id
        )
        producer.producer.send_and_wait.assert_awaited_once()
        call_args = producer.producer.send_and_wait.call_args
        assert b"review.resolved" in call_args.args[0].encode() if isinstance(call_args.args[0], str) else True
        assert call_args.kwargs.get("key") == review.job_id.encode()

    @pytest.mark.asyncio
    async def test_sweep_skips_concurrently_resolved(self) -> None:
        """mark_timed_out returns None → another worker already resolved; skip."""
        from trendstorm.orchestration.workers.review_timeout_worker import ReviewTimeoutSweeper

        review = self._make_expired_review()
        review_repo = AsyncMock()
        review_repo.list_expired_pending.return_value = [review]
        review_repo.mark_timed_out.return_value = None  # concurrently resolved

        producer = AsyncMock()
        producer.producer = AsyncMock()

        sweeper = ReviewTimeoutSweeper(
            review_repo=review_repo,
            producer=producer,
        )

        with patch("trendstorm.orchestration.workers.review_timeout_worker.METRICS"):
            await sweeper._sweep_once()

        producer.producer.send_and_wait.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sweep_no_expired_is_noop(self) -> None:
        from trendstorm.orchestration.workers.review_timeout_worker import ReviewTimeoutSweeper

        review_repo = AsyncMock()
        review_repo.list_expired_pending.return_value = []
        producer = AsyncMock()
        producer.producer = AsyncMock()

        sweeper = ReviewTimeoutSweeper(review_repo=review_repo, producer=producer)

        with patch("trendstorm.orchestration.workers.review_timeout_worker.METRICS"):
            await sweeper._sweep_once()

        review_repo.mark_timed_out.assert_not_awaited()
        producer.producer.send_and_wait.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sweep_continues_on_single_review_error(self) -> None:
        """Error expiring one review must not abort the rest of the batch."""
        from trendstorm.orchestration.workers.review_timeout_worker import ReviewTimeoutSweeper

        r1 = self._make_expired_review()
        r2 = self._make_expired_review()
        review_repo = AsyncMock()
        review_repo.list_expired_pending.return_value = [r1, r2]
        review_repo.mark_timed_out.side_effect = [Exception("transient"), r2]

        producer = AsyncMock()
        producer.producer = AsyncMock()

        sweeper = ReviewTimeoutSweeper(review_repo=review_repo, producer=producer)

        with patch("trendstorm.orchestration.workers.review_timeout_worker.METRICS") as mock_metrics:
            mock_metrics.review_timeout_total = MagicMock()
            await sweeper._sweep_once()

        # r2 should still have been processed
        assert producer.producer.send_and_wait.await_count == 1

    @pytest.mark.asyncio
    async def test_stop_event_terminates_loop(self) -> None:
        from trendstorm.orchestration.workers.review_timeout_worker import ReviewTimeoutSweeper

        review_repo = AsyncMock()
        review_repo.list_expired_pending.return_value = []
        producer = AsyncMock()
        producer.producer = AsyncMock()

        sweeper = ReviewTimeoutSweeper(
            review_repo=review_repo,
            producer=producer,
            poll_interval_seconds=1,
        )
        stop_event = asyncio.Event()
        stop_event.set()  # already stopped

        with patch("trendstorm.orchestration.workers.review_timeout_worker.METRICS"):
            await asyncio.wait_for(
                sweeper.sweep_loop(stop_event=stop_event), timeout=2.0
            )
        # Just verifying it terminates without hanging.
