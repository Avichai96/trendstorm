"""@with_metrics — decorator that records the Four Golden Signals.

Records on every async function call:
    - latency (histogram)
    - throughput (counter increment on call)
    - errors (counter increment with error_class label on exception)
    - saturation (caller supplies a gauge; the decorator does not own it)

Usage:
    from trendstorm.shared.metrics.decorators import with_metrics
    from trendstorm.shared.metrics.registry import METRICS

    @with_metrics(
        duration_metric=METRICS.scout_source_duration,
        counter_metric=METRICS.scout_sources,
        labels={"tenant_id": lambda args: args[0].tenant_id},
    )
    async def fetch_source(self, source: Source) -> FetchResult:
        ...

Design notes:
    - Labels are late-bound via callables so the decorator doesn't need to
      know the function's argument structure at decoration time.
    - On exception: status="error" or "permanent_error" depending on the
      exception class matching `permanent_classes`.
    - The exception is ALWAYS re-raised; the decorator only observes.
    - Sync functions are not supported (all TrendStorm handlers are async).
"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import time
from collections.abc import Callable
from typing import Any

from prometheus_client import Counter, Histogram


def _select_labels(metric: Any, label_values: dict[str, str]) -> dict[str, str]:
    """Return only the labels declared on metric, ignoring extras.

    Each metric declares its own label set (e.g. duration may only have
    tenant_id+status while the counter has tenant_id+content_type+status).
    Passing undeclared labels to prometheus_client raises ValueError; this
    helper filters to the intersection so the decorator works for any pairing.
    """
    declared = set(metric._labelnames)  # prometheus_client internal attribute; stubs now expose it
    return {k: v for k, v in label_values.items() if k in declared}


def with_metrics(
    *,
    duration_metric: Histogram,
    counter_metric: Counter,
    labels: dict[str, str | Callable[..., str]],
    permanent_classes: tuple[type[Exception], ...] = (),
) -> Callable[..., Any]:
    """Record latency and count on the decorated coroutine.

    Args:
        duration_metric: Histogram to observe elapsed seconds.
        counter_metric: Counter to increment on each call.
        labels: Dict mapping label name → either a static string or a callable
                that receives (args, kwargs) of the decorated function and
                returns the label value.
        permanent_classes: Exception types to label as "permanent_error" rather
                           than the generic "error". TrendStorm permanent errors
                           inherit from TrendStormError; pass specific subclasses.

    """
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        if not asyncio.iscoroutinefunction(fn):
            raise TypeError(
                f"@with_metrics only supports async functions; got {fn!r}"
            )

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            resolved: dict[str, str] = {}
            for label, value in labels.items():
                resolved[label] = value(args, kwargs) if callable(value) else value

            start = time.perf_counter()
            status = "success"
            try:
                return await fn(*args, **kwargs)
            except permanent_classes:
                status = "permanent_error"
                raise
            except Exception:
                status = "error"
                raise
            finally:
                elapsed = time.perf_counter() - start
                all_labels = {**resolved, "status": status}
                with contextlib.suppress(Exception):
                    # Metric recording must never crash the business logic.
                    dur_labels = _select_labels(duration_metric, all_labels)
                    duration_metric.labels(**dur_labels).observe(elapsed)
                    cnt_labels = _select_labels(counter_metric, all_labels)
                    counter_metric.labels(**cnt_labels).inc()

        return wrapper
    return decorator
