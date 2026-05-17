"""Unit tests for shared/metrics/cost.py — LLM cost attribution counters."""
from __future__ import annotations

import pytest

from trendstorm.shared.metrics.cost import record_llm_cost
from trendstorm.shared.metrics.registry import make_test_metrics

pytestmark = pytest.mark.unit


def _get_counter_value(counter, **labels) -> float:
    return counter.labels(**labels)._value.get()  # type: ignore[attr-defined]


class TestRecordLlmCost:
    def test_increments_input_tokens(self, monkeypatch) -> None:
        m = make_test_metrics()
        monkeypatch.setattr("trendstorm.shared.metrics.cost.METRICS", m)

        record_llm_cost(
            tenant_id="t1",
            provider="anthropic",
            model_id="claude-3-5-sonnet-20241022",
            operation="analyst_chat",
            input_tokens=1000,
            output_tokens=200,
        )

        v = _get_counter_value(
            m.llm_input_tokens,
            tenant_id="t1",
            provider="anthropic",
            model_id="claude-3-5-sonnet-20241022",
            operation="analyst_chat",
        )
        assert v == 1000.0

    def test_increments_output_tokens(self, monkeypatch) -> None:
        m = make_test_metrics()
        monkeypatch.setattr("trendstorm.shared.metrics.cost.METRICS", m)

        record_llm_cost(
            tenant_id="t1",
            provider="openai",
            model_id="gpt-4o-mini",
            operation="query_expansion",
            input_tokens=50,
            output_tokens=30,
        )

        v = _get_counter_value(
            m.llm_output_tokens,
            tenant_id="t1",
            provider="openai",
            model_id="gpt-4o-mini",
            operation="query_expansion",
        )
        assert v == 30.0

    def test_increments_cached_tokens_when_provided(self, monkeypatch) -> None:
        m = make_test_metrics()
        monkeypatch.setattr("trendstorm.shared.metrics.cost.METRICS", m)

        record_llm_cost(
            tenant_id="t1",
            provider="anthropic",
            model_id="claude-3-5-haiku-20241022",
            operation="validator_chat",
            input_tokens=800,
            output_tokens=100,
            cached_tokens=600,
        )

        v = _get_counter_value(
            m.llm_cached_tokens,
            tenant_id="t1",
            provider="anthropic",
            model_id="claude-3-5-haiku-20241022",
            operation="validator_chat",
        )
        assert v == 600.0

    def test_skips_cached_tokens_when_zero(self, monkeypatch) -> None:
        m = make_test_metrics()
        monkeypatch.setattr("trendstorm.shared.metrics.cost.METRICS", m)

        record_llm_cost(
            tenant_id="t1",
            provider="openai",
            model_id="gpt-4o",
            operation="analyst_chat",
            input_tokens=500,
            output_tokens=100,
            cached_tokens=0,
        )

        v = _get_counter_value(
            m.llm_cached_tokens,
            tenant_id="t1",
            provider="openai",
            model_id="gpt-4o",
            operation="analyst_chat",
        )
        # zero cached_tokens → counter not incremented → stays at 0
        assert v == 0.0

    def test_success_call_increments_call_counter_with_success_status(self, monkeypatch) -> None:
        m = make_test_metrics()
        monkeypatch.setattr("trendstorm.shared.metrics.cost.METRICS", m)

        record_llm_cost(
            tenant_id="t2",
            provider="gemini",
            model_id="gemini-1.5-flash",
            operation="embed_document",
            input_tokens=200,
            output_tokens=0,
            success=True,
        )

        v = _get_counter_value(
            m.llm_calls,
            tenant_id="t2",
            provider="gemini",
            model_id="gemini-1.5-flash",
            operation="embed_document",
            status="success",
        )
        assert v == 1.0

    def test_failed_call_records_permanent_error_status(self, monkeypatch) -> None:
        m = make_test_metrics()
        monkeypatch.setattr("trendstorm.shared.metrics.cost.METRICS", m)

        record_llm_cost(
            tenant_id="t3",
            provider="anthropic",
            model_id="claude-3-5-sonnet-20241022",
            operation="analyst_chat",
            input_tokens=100,
            output_tokens=0,
            success=False,
        )

        v = _get_counter_value(
            m.llm_calls,
            tenant_id="t3",
            provider="anthropic",
            model_id="claude-3-5-sonnet-20241022",
            operation="analyst_chat",
            status="permanent_error",
        )
        assert v == 1.0

    def test_duration_observed_when_provided(self, monkeypatch) -> None:
        m = make_test_metrics()
        monkeypatch.setattr("trendstorm.shared.metrics.cost.METRICS", m)

        record_llm_cost(
            tenant_id="t1",
            provider="anthropic",
            model_id="claude-3-5-sonnet-20241022",
            operation="analyst_chat",
            input_tokens=100,
            output_tokens=50,
            call_duration_seconds=2.5,
        )

        # Verify duration was observed (sum should be 2.5)
        hist = m.llm_call_duration.labels(
            tenant_id="t1",
            provider="anthropic",
            model_id="claude-3-5-sonnet-20241022",
            operation="analyst_chat",
        )
        assert hist._sum.get() == pytest.approx(2.5)  # type: ignore[attr-defined]

    def test_accumulates_across_multiple_calls(self, monkeypatch) -> None:
        m = make_test_metrics()
        monkeypatch.setattr("trendstorm.shared.metrics.cost.METRICS", m)

        for _ in range(3):
            record_llm_cost(
                tenant_id="t1",
                provider="openai",
                model_id="gpt-4o-mini",
                operation="analyst_chat",
                input_tokens=100,
                output_tokens=50,
            )

        v = _get_counter_value(
            m.llm_input_tokens,
            tenant_id="t1",
            provider="openai",
            model_id="gpt-4o-mini",
            operation="analyst_chat",
        )
        assert v == 300.0

    def test_metric_failure_does_not_raise(self, monkeypatch) -> None:
        """Recording failures must not crash the caller."""
        from unittest.mock import MagicMock
        m = make_test_metrics()
        m.llm_calls = MagicMock(side_effect=RuntimeError("prometheus down"))
        monkeypatch.setattr("trendstorm.shared.metrics.cost.METRICS", m)

        # Should complete silently.
        record_llm_cost(
            tenant_id="t1",
            provider="anthropic",
            model_id="claude-3-5-sonnet-20241022",
            operation="analyst_chat",
            input_tokens=100,
            output_tokens=50,
        )
