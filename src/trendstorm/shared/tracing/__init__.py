"""OpenTelemetry tracing configuration.

Strategy:
    - Auto-instrumentation for HTTP frameworks, DB drivers, HTTP clients.
    - Manual span creation only for business-meaningful operations.
    - All telemetry exports via OTLP to the Collector (Phase 2).
    - Sampling: 100% in dev, configurable in prod (parentbased_traceidratio).

Why a TracerProvider per service?
    - Each deployable unit (api, scout, analyst) gets its own
      `service.name` resource attribute.
    - Spans across services correlate via trace_id (propagated through headers
      and Kafka message headers).

When to add a manual span?
    - You're entering a unit of work that doesn't correspond to a single
      I/O call (e.g. "run_hybrid_retrieval", "evaluate_analyst_output").
    - You want to attach attributes that downstream debugging will need
      (e.g. token counts, retrieval scores).
"""

from __future__ import annotations

from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.instrumentation.pymongo import PymongoInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.semconv.resource import ResourceAttributes
from opentelemetry.trace import Tracer

from trendstorm.shared.config import get_settings

_initialized: bool = False


def configure_tracing(service_name: str | None = None) -> None:
    """Initialize the global TracerProvider and install auto-instrumentors.

    Call once per process at startup, after config and logging are set up.

    Args:
        service_name: Override the service.name attribute. Useful when the same
            codebase ships multiple binaries (api, scout-worker, etc.). If None,
            uses settings.otel.service_name.

    """
    global _initialized
    if _initialized:
        return

    settings = get_settings()

    # Resource attributes are attached to every span emitted by this process.
    # service.name is the most important one — it's how Jaeger/Tempo groups
    # spans into services.
    resource = Resource.create(
        {
            ResourceAttributes.SERVICE_NAME: service_name or settings.otel.service_name,
            ResourceAttributes.SERVICE_VERSION: settings.otel.service_version,
            ResourceAttributes.DEPLOYMENT_ENVIRONMENT: settings.app.env.value,
        }
    )

    # Sampler: ParentBased + TraceIdRatio is the standard recipe.
    #   - If the incoming request has trace context, respect its sampling decision.
    #   - Otherwise, sample new traces at the configured ratio.
    # This ensures a trace is either fully sampled or fully dropped — never
    # half-sampled, which would be useless for debugging.
    sampler = ParentBased(root=TraceIdRatioBased(settings.otel.traces_sampler_arg))

    provider = TracerProvider(resource=resource, sampler=sampler)

    # BatchSpanProcessor buffers spans and exports in batches.
    # Trade-off: spans are not immediately visible, but throughput is much higher.
    # In tests, you'd use SimpleSpanProcessor for synchronous export.
    exporter = OTLPSpanExporter(
        endpoint=settings.otel.exporter_otlp_endpoint,
        insecure=True,  # local dev; in prod use TLS with proper certs
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(provider)

    # ---- Auto-instrumentation ----
    # Each instrumentor monkey-patches the target library to create spans.
    # IMPORTANT: instrument BEFORE the library is heavily used so existing
    # objects don't escape instrumentation.

    # FastAPI: creates a span per request, sets http.* attributes.
    # NOTE: FastAPIInstrumentor is also invoked from main.py against the app
    # instance — that path handles per-request context. Calling it here is
    # harmless (it's idempotent at the global level).

    # PymongoInstrumentor instruments motor too (motor wraps pymongo).
    PymongoInstrumentor().instrument()

    # Redis: spans per command (GET, SET, etc.) with redis.command attribute.
    RedisInstrumentor().instrument()

    # HTTPX: spans per outgoing HTTP call.
    HTTPXClientInstrumentor().instrument()

    # Logging integration: injects trace_id/span_id into stdlib log records
    # so libraries' logs are correlated. (We also do this in structlog
    # processors, but this catches everything.)
    LoggingInstrumentor().instrument(set_logging_format=False)

    _initialized = True


def instrument_fastapi(app: Any) -> None:
    """Apply FastAPI auto-instrumentation to an app instance.

    Must be called AFTER configure_tracing() and ideally BEFORE the app is
    served, so all routes get instrumented.
    """
    FastAPIInstrumentor.instrument_app(
        app,
        # Don't trace these — they're noisy and not useful for debugging.
        excluded_urls="/health/live,/health/ready,/metrics",
    )


def get_tracer(name: str) -> Tracer:
    """Get a tracer for manual span creation.

    Convention: pass the module's __name__ as the tracer name. The library
    docs recommend using the instrumented package name, but __name__ gives
    us per-module granularity in Jaeger.
    """
    return trace.get_tracer(name)


def shutdown_tracing() -> None:
    """Flush pending spans and shut down the provider.

    Call from the app's lifespan shutdown hook. Without this, the last few
    seconds of spans may be lost when the process exits.
    """
    provider = trace.get_tracer_provider()
    # Type narrowing — only the SDK provider has shutdown()
    if isinstance(provider, TracerProvider):
        provider.shutdown()


def business_span(name: str, **attributes: Any) -> Any:
    """Context manager for a business-meaningful span.

    Convenience wrapper around tracer.start_as_current_span that:
    - Uses the module-level tracer (no per-caller tracer needed for simple spans).
    - Accepts keyword arguments as span attributes.
    - Attribute keys MUST come from shared.tracing.semantics.Attr constants.

    Usage:
        from trendstorm.shared.tracing import business_span
        from trendstorm.shared.tracing.semantics import Attr

        async with business_span("scout.fetch_source",
                                 **{Attr.TENANT_ID: tid, Attr.SOURCE_ID: sid}):
            ...

    For spans that need the span object (to set status or add events), use
    `tracer.start_as_current_span()` directly.
    """
    _tracer = trace.get_tracer("trendstorm.business")
    return _tracer.start_as_current_span(name, attributes=attributes)
