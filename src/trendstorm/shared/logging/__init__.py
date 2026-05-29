"""Structured logging configuration.

Architecture:
    - structlog as the API (rich, context-aware).
    - Standard logging library as the backend (interop with libraries).
    - JSON output in production, pretty colored in local dev.
    - Correlation IDs and OTel trace IDs auto-injected via contextvars.

Usage:
    from trendstorm.shared.logging import get_logger, bind_context

    logger = get_logger(__name__)

    async def handler(request):
        # Bind once at the entry point; all downstream logs inherit context
        bind_context(correlation_id=request.headers["x-correlation-id"])
        logger.info("processing", path=request.url.path)

Output (JSON):
    {
      "event": "processing",
      "level": "info",
      "logger": "trendstorm.api.routers.jobs",
      "timestamp": "2026-05-17T10:30:45.123Z",
      "correlation_id": "01HZ...",
      "trace_id": "abc123...",
      "span_id": "def456...",
      "path": "/jobs"
    }
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any, Final

import structlog
from opentelemetry import trace
from structlog.types import EventDict, Processor

from trendstorm.shared.config import LogFormat, get_settings

# ---------------------------------------------------------------------------
# Context vars carry request-scoped state across async boundaries.
# Unlike thread-locals, contextvars are correctly propagated by asyncio.
# ---------------------------------------------------------------------------

_correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)
_tenant_id_var: ContextVar[str | None] = ContextVar("tenant_id", default=None)
_user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)


def bind_context(
    *,
    correlation_id: str | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
) -> None:
    """Bind request-scoped values into the logging context.

    Call this at the entry point of a request (middleware) or job (worker).
    All subsequent log calls in the same async context inherit these fields.
    """
    if correlation_id is not None:
        _correlation_id_var.set(correlation_id)
    if tenant_id is not None:
        _tenant_id_var.set(tenant_id)
    if user_id is not None:
        _user_id_var.set(user_id)


def get_correlation_id() -> str | None:
    """Read the current correlation ID, if any."""
    return _correlation_id_var.get()


# ---------------------------------------------------------------------------
# Structlog processors — the pipeline each log record flows through.
# Order matters: each processor can mutate the event_dict.
# ---------------------------------------------------------------------------


def _add_log_context(_: Any, __: str, event_dict: EventDict) -> EventDict:
    """Inject contextvars (correlation_id, tenant_id, user_id) into every log."""
    if (cid := _correlation_id_var.get()) is not None:
        event_dict["correlation_id"] = cid
    if (tid := _tenant_id_var.get()) is not None:
        event_dict["tenant_id"] = tid
    if (uid := _user_id_var.get()) is not None:
        event_dict["user_id"] = uid
    return event_dict


def _add_otel_context(_: Any, __: str, event_dict: EventDict) -> EventDict:
    """Inject OTel trace_id and span_id so logs are searchable by trace.

    This is what enables the Loki → Jaeger cross-linking we configured in
    Phase 2's Grafana datasources.
    """
    span = trace.get_current_span()
    if not span.is_recording():
        return event_dict
    ctx = span.get_span_context()
    if ctx.is_valid:
        # OTel uses 128-bit trace IDs as ints; format as 32-char hex to match
        # the format Grafana/Jaeger UIs use.
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


def _drop_color_message_key(_: Any, __: str, event_dict: EventDict) -> EventDict:
    """Structlog's stdlib formatter adds 'color_message' for terminals; drop in JSON."""
    event_dict.pop("color_message", None)
    return event_dict


# Sensitive header/field names that must never appear in logs.
_SENSITIVE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "api_key",
        "api-key",
        "x-api-key",
        "password",
        "secret",
        "token",
        "anthropic_api_key",
        "openai_api_key",
        "cohere_api_key",
        "mongo_uri",
        "redis_url",
    }
)


def _redact_sensitive(_: Any, __: str, event_dict: EventDict) -> EventDict:
    """Replace sensitive values with '***' before serialization.

    Case-insensitive key match. This is a defense-in-depth measure;
    primary defense is using SecretStr in config.
    """
    for key in list(event_dict.keys()):
        if key.lower() in _SENSITIVE_KEYS:
            event_dict[key] = "***"
    return event_dict


# ---------------------------------------------------------------------------
# Configuration entry point — call once at app startup
# ---------------------------------------------------------------------------


def configure_logging() -> None:
    """Configure structlog and stdlib logging.

    Idempotent: safe to call multiple times. The first call wins;
    subsequent calls reconfigure but don't break existing loggers.
    """
    settings = get_settings()
    log_level = getattr(logging, settings.app.log_level.value)

    # Shared processor chain applied to BOTH structlog calls AND stdlib calls
    # (the latter via the ProcessorFormatter pattern).
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.stdlib.add_logger_name,
        _add_log_context,
        _add_otel_context,
        _redact_sensitive,
    ]

    # Final renderer differs by environment
    if settings.app.log_format == LogFormat.JSON:
        renderer: Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(
            colors=True, exception_formatter=structlog.dev.RichTracebackFormatter()
        )

    # Structlog config — used by code that imports structlog directly
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _drop_color_message_key,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Stdlib logging config — captures logs from libraries (motor, aiokafka, etc.)
    # and routes them through the same renderer for consistent output.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            _drop_color_message_key,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()  # remove default handlers (avoid duplicate output)
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level)

    # Tame noisy libraries — they log at INFO by default but we rarely care.
    for noisy in ("uvicorn.access", "httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # uvicorn re-attaches its own handler if we don't tell it not to.
    for uv in ("uvicorn", "uvicorn.error"):
        logging.getLogger(uv).handlers.clear()
        logging.getLogger(uv).propagate = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get a structured logger.

    Args:
        name: Logger name; defaults to caller's module via __name__ if None.

    Convention: every module gets `logger = get_logger(__name__)` at module top.

    """
    return structlog.stdlib.get_logger(name)
