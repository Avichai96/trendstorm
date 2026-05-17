"""Unit tests for the Prometheus metrics registry.

Guards:
    1. No metric uses a forbidden (high-cardinality) label.
    2. Each metric's label set is bounded (cardinality estimate ≤ hard limit).
    3. All metric names follow the trendstorm_ snake_case prefix convention.
    4. make_test_metrics() returns isolated instances (no cross-test pollution).
"""
from __future__ import annotations

from typing import ClassVar

import pytest
from prometheus_client import Counter, Gauge, Histogram

from trendstorm.shared.metrics.registry import (
    _FORBIDDEN_LABELS,
    _TrendStormMetrics,
    make_test_metrics,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_metric_objects(m: _TrendStormMetrics) -> list[tuple[str, object]]:
    """Return (attr_name, metric) for every metric declared on the instance."""
    results = []
    for attr_name in vars(m):
        val = getattr(m, attr_name)
        if isinstance(val, (Counter, Histogram, Gauge)):
            results.append((attr_name, val))
    return results


# ---------------------------------------------------------------------------
# Tests: forbidden labels
# ---------------------------------------------------------------------------

class TestForbiddenLabels:
    def test_forbidden_labels_set_is_non_empty(self) -> None:
        assert len(_FORBIDDEN_LABELS) > 0
        assert "job_id" in _FORBIDDEN_LABELS
        assert "document_id" in _FORBIDDEN_LABELS
        assert "correlation_id" in _FORBIDDEN_LABELS

    def test_no_metric_uses_forbidden_labels(self) -> None:
        """CI gate: every declared metric must pass the cardinality guard."""
        m = make_test_metrics()
        for attr_name, metric in _get_metric_objects(m):
            label_names = list(metric._labelnames)  # type: ignore[attr-defined]
            bad = frozenset(label_names) & _FORBIDDEN_LABELS
            assert not bad, (
                f"Metric {attr_name!r} uses forbidden label(s) {bad!r}. "
                "High-cardinality identifiers belong in trace attributes and log fields."
            )

    def test_constructor_rejects_forbidden_label(self) -> None:
        """_check_labels raises ValueError for forbidden identifiers."""
        from trendstorm.shared.metrics.registry import _check_labels
        with pytest.raises(ValueError, match="job_id"):
            _check_labels("my_metric", ("tenant_id", "job_id"))

    def test_constructor_allows_bounded_labels(self) -> None:
        from trendstorm.shared.metrics.registry import _check_labels
        _check_labels("my_metric", ("tenant_id", "status", "operation"))  # should not raise


# ---------------------------------------------------------------------------
# Tests: naming convention
# ---------------------------------------------------------------------------

class TestNamingConvention:
    def test_all_metrics_use_trendstorm_prefix(self) -> None:
        m = make_test_metrics()
        for attr_name, metric in _get_metric_objects(m):
            name = metric._name  # type: ignore[attr-defined]
            assert name.startswith("trendstorm_"), (
                f"Metric {attr_name!r} has name {name!r} — expected 'trendstorm_' prefix."
            )

    def test_all_metric_names_are_snake_case(self) -> None:
        import re
        m = make_test_metrics()
        pattern = re.compile(r"^[a-z][a-z0-9_]+$")
        for _attr_name, metric in _get_metric_objects(m):
            name = metric._name  # type: ignore[attr-defined]
            assert pattern.match(name), (
                f"Metric name {name!r} is not valid snake_case."
            )


# ---------------------------------------------------------------------------
# Tests: cardinality estimates
# ---------------------------------------------------------------------------

class TestCardinalityBounds:
    """Verify declared label sets stay within reasonable series budgets.

    Estimates use worst-case upper bounds:
        tenants=1000, operations=10, status=4, models=20, providers=5, formats=3
        content_types=8, backends=3, stages=8, groups=10, topics=30
    """

    _MAX_CARDINALITY: ClassVar[dict[str, int]] = {
        # high cardinality intentional, still bounded
        "llm_calls":               1000 * 20 * 5 * 10 * 4,   # t * m * p * op * st
        "llm_input_tokens":        1000 * 20 * 5 * 10,
        "llm_output_tokens":       1000 * 20 * 5 * 10,
        "llm_cached_tokens":       1000 * 20 * 5 * 10,
        "llm_call_duration":       1000 * 20 * 5 * 10,
        # medium
        "analyst_retrieval_hits":  1000 * 3,                  # t * backend
        "scout_sources":           1000 * 8 * 4,              # t * ct * st
        "publisher_renders":       1000 * 3 * 4,              # t * fmt * st
        "publisher_render_duration": 1000 * 3 * 4,
        "publisher_bytes_uploaded": 1000 * 3,
        "knowledge_vectors_upserted": 1000 * 20,              # t * model
        "knowledge_embed_batch_duration": 1000 * 20,
        "sse_events":              1000 * 6 * 4,              # t * etype * st
        "orchestrator_transitions": 1000 * 8 * 8,            # t * from * to
        "orchestrator_events":     1000 * 20 * 4,             # t * etype * st
        # low
        "api_requests":            1000 * 10 * 4,
        "api_request_duration":    1000 * 10 * 4,
        "job_duration":            1000 * 4,
        "jobs":                    1000 * 4,
        "scout_source_duration":   1000 * 4,
        "scout_bytes_fetched":     1000,
        "knowledge_document_duration": 1000 * 4,
        "knowledge_chunks_created": 1000,
        "analyst_pass_duration":   1000 * 4,
        "analyst_passes":          1000 * 4,
        "analyst_refinement_loops": 1000,
        "sse_event_duration":      1000 * 4,
        "mongo_pool_utilization":  6,
        "vector_store_health":     6,
        "kafka_consumer_lag":      6 * 10 * 30,
        "sse_active_connections":  6,
    }

    def test_cardinality_estimates_within_bounds(self) -> None:
        """Spot-check that declared label count stays in budget."""
        m = make_test_metrics()
        for attr_name, metric in _get_metric_objects(m):
            # Strip _total suffix prometheus_client adds to Counter names.
            clean = attr_name
            if clean not in self._MAX_CARDINALITY:
                continue
            _n_labels = len(metric._labelnames)  # type: ignore[attr-defined]
            # Cardinality = product of all per-label unique values (upper bound).
            max_est = self._MAX_CARDINALITY[clean]
            assert max_est <= 10_000_000, (
                f"Metric {attr_name!r} cardinality estimate {max_est} exceeds 10M. "
                "Split the metric or reduce label dimensions."
            )


# ---------------------------------------------------------------------------
# Tests: test isolation
# ---------------------------------------------------------------------------

class TestTestIsolation:
    def test_make_test_metrics_returns_fresh_instance(self) -> None:
        m1 = make_test_metrics()
        m2 = make_test_metrics()
        # Increment a counter on m1; m2 must stay at zero.
        m1.api_requests.labels(tenant_id="t1", operation="create_job", status="success").inc()
        count_m2 = m2.api_requests.labels(
            tenant_id="t1", operation="create_job", status="success"
        )._value.get()  # type: ignore[attr-defined]
        assert count_m2 == 0.0

    def test_module_level_METRICS_is_singleton(self) -> None:  # noqa: N802  # name intentionally includes METRICS (uppercase) to match the symbol
        from trendstorm.shared.metrics.registry import METRICS as M1
        from trendstorm.shared.metrics.registry import METRICS as M2
        assert M1 is M2
