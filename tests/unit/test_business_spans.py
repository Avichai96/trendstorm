"""Unit tests for business span helpers and semantics constants.

Uses a fake in-memory OTel tracer so no real OTLP export happens.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from trendstorm.shared.tracing.semantics import Attr

pytestmark = pytest.mark.unit


@pytest.fixture
def span_exporter():
    """Inject a test TracerProvider into business_span via patching.

    The OTel global TracerProvider cannot be safely overridden once set (the SDK
    ignores the override in recent versions). We instead patch trace.get_tracer
    inside the shared.tracing module so business_span uses our test provider.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    test_tracer = provider.get_tracer("test")

    with patch("trendstorm.shared.tracing.trace") as mock_trace:
        mock_trace.get_tracer.return_value = test_tracer
        # business_span calls: _tracer = trace.get_tracer("trendstorm.business")
        # and then _tracer.start_as_current_span(name, attributes=...)
        yield exporter

    exporter.clear()


class TestAttrConstants:
    def test_all_attrs_are_strings(self) -> None:
        for name in vars(Attr):
            if name.startswith("_"):
                continue
            val = getattr(Attr, name)
            assert isinstance(val, str), f"Attr.{name} must be a str, got {type(val)}"

    def test_high_cardinality_attrs_use_trendstorm_prefix_or_semconv(self) -> None:
        """High-cardinality IDs (job_id etc.) use trendstorm. or otel semconv prefix."""
        for attr in [Attr.JOB_ID, Attr.DOCUMENT_ID, Attr.CHUNK_ID, Attr.TENANT_ID]:
            assert attr.startswith("trendstorm.") or attr.startswith("http."), (
                f"{attr!r} should use 'trendstorm.' or OTel semconv prefix"
            )

    def test_no_duplicate_values(self) -> None:
        """Every attribute key must be unique."""
        values = [
            getattr(Attr, name)
            for name in vars(Attr)
            if not name.startswith("_")
        ]
        assert len(values) == len(set(values)), (
            f"Duplicate Attr values: {[v for v in values if values.count(v) > 1]}"
        )

    def test_token_attrs_exist(self) -> None:
        assert Attr.INPUT_TOKENS == "trendstorm.input_tokens"
        assert Attr.OUTPUT_TOKENS == "trendstorm.output_tokens"
        assert Attr.CACHED_TOKENS == "trendstorm.cached_tokens"

    def test_retrieval_funnel_attrs_exist(self) -> None:
        assert Attr.BM25_HITS == "trendstorm.bm25_hits"
        assert Attr.VECTOR_HITS == "trendstorm.vector_hits"
        assert Attr.AFTER_RRF_COUNT == "trendstorm.after_rrf_count"
        assert Attr.AFTER_RERANK_COUNT == "trendstorm.after_rerank_count"


class TestBusinessSpan:
    def test_creates_span_with_correct_name(self, span_exporter) -> None:
        from trendstorm.shared.tracing import business_span

        with business_span("scout.fetch_source"):
            pass

        spans = span_exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].name == "scout.fetch_source"

    def test_span_attributes_are_set(self, span_exporter) -> None:
        from trendstorm.shared.tracing import business_span

        with business_span(
            "knowledge.embed_batch",
            **{
                Attr.TENANT_ID: "t1",
                Attr.BATCH_SIZE: 16,
                Attr.EMBEDDING_MODEL: "nomic-embed-text",
            },
        ):
            pass

        spans = span_exporter.get_finished_spans()
        assert len(spans) == 1
        attrs = spans[0].attributes
        assert attrs[Attr.TENANT_ID] == "t1"  # type: ignore[index]
        assert attrs[Attr.BATCH_SIZE] == 16    # type: ignore[index]
        assert attrs[Attr.EMBEDDING_MODEL] == "nomic-embed-text"  # type: ignore[index]

    def test_span_ends_on_exception(self, span_exporter) -> None:
        from trendstorm.shared.tracing import business_span

        with pytest.raises(ValueError), business_span("analyst.llm_call"):
            raise ValueError("test error")

        spans = span_exporter.get_finished_spans()
        assert len(spans) == 1  # span was ended despite exception

    def test_nested_spans_are_independent(self, span_exporter) -> None:
        from trendstorm.shared.tracing import business_span

        with business_span("outer.span"), business_span("inner.span"):
            pass

        spans = span_exporter.get_finished_spans()
        names = {s.name for s in spans}
        assert "outer.span" in names
        assert "inner.span" in names


@pytest.mark.asyncio
class TestWithMetricsDecorator:
    """Unit tests for the @with_metrics decorator."""

    async def test_records_success_metrics(self) -> None:
        from trendstorm.shared.metrics.decorators import with_metrics
        from trendstorm.shared.metrics.registry import make_test_metrics

        m = make_test_metrics()

        @with_metrics(
            duration_metric=m.scout_source_duration,
            counter_metric=m.scout_sources,
            labels={
                "tenant_id": lambda args, _: "t1",
                "content_type": "html",
            },
        )
        async def fake_fetch():
            return "ok"

        await fake_fetch()

        count = m.scout_sources.labels(
            tenant_id="t1", content_type="html", status="success"
        )._value.get()  # type: ignore[attr-defined]
        assert count == 1.0

    async def test_records_error_on_exception(self) -> None:
        from trendstorm.shared.metrics.decorators import with_metrics
        from trendstorm.shared.metrics.registry import make_test_metrics

        m = make_test_metrics()

        @with_metrics(
            duration_metric=m.scout_source_duration,
            counter_metric=m.scout_sources,
            labels={
                "tenant_id": lambda args, _: "t2",
                "content_type": "html",
            },
        )
        async def broken():
            raise RuntimeError("oops")

        with pytest.raises(RuntimeError):
            await broken()

        count = m.scout_sources.labels(
            tenant_id="t2", content_type="html", status="error"
        )._value.get()  # type: ignore[attr-defined]
        assert count == 1.0

    async def test_permanent_error_label(self) -> None:
        from trendstorm.shared.errors import NotFoundError
        from trendstorm.shared.metrics.decorators import with_metrics
        from trendstorm.shared.metrics.registry import make_test_metrics

        m = make_test_metrics()

        @with_metrics(
            duration_metric=m.analyst_pass_duration,
            counter_metric=m.analyst_passes,
            labels={"tenant_id": lambda args, _: "t1"},
            permanent_classes=(NotFoundError,),
        )
        async def analyst_fail():
            raise NotFoundError("analysis not found")

        with pytest.raises(NotFoundError):
            await analyst_fail()

        count = m.analyst_passes.labels(
            tenant_id="t1", status="permanent_error"
        )._value.get()  # type: ignore[attr-defined]
        assert count == 1.0


@pytest.mark.unit
def test_with_metrics_rejects_sync_functions() -> None:
    from trendstorm.shared.metrics.decorators import with_metrics
    from trendstorm.shared.metrics.registry import make_test_metrics

    m = make_test_metrics()

    with pytest.raises(TypeError, match="async"):
        @with_metrics(
            duration_metric=m.scout_source_duration,
            counter_metric=m.scout_sources,
            labels={"tenant_id": "t1", "content_type": "html"},
        )
        def sync_fn():
            pass
