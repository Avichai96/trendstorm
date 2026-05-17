"""Centralized exception handlers.

Maps domain exceptions to HTTP responses with a stable error envelope:
    {
      "error": {
        "code": "not_found",
        "message": "...",
        "context": {...}
      },
      "correlation_id": "01HZ..."
    }

The envelope is contractual: SDKs and frontends depend on it.

Why centralize?
    - Routes never write `try/except -> return JSONResponse(...)` boilerplate.
    - One place to add things like Sentry reporting, error metrics, etc.
    - Consistent shape across the entire API.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from trendstorm.shared.errors import (
    BusinessRuleError,
    ConflictError,
    ExternalServiceError,
    NotFoundError,
    TrendStormError,
    ValidationError,
)
from trendstorm.shared.errors import ValidationError as DomainValidationError
from trendstorm.shared.logging import get_correlation_id, get_logger

logger = get_logger(__name__)


def _envelope(error: dict[str, Any]) -> dict[str, Any]:
    """Wrap an error dict with the standard envelope."""
    return {
        "error": error,
        "correlation_id": get_correlation_id(),
    }


# ---------------------------------------------------------------------------
# Per-exception-type handlers
# ---------------------------------------------------------------------------

async def domain_error_handler(_: Request, exc: TrendStormError) -> JSONResponse:
    """Map TrendStormError subclasses to appropriate HTTP statuses."""
    if isinstance(exc, NotFoundError):
        http_status = status.HTTP_404_NOT_FOUND
    elif isinstance(exc, (DomainValidationError, ValidationError)):
        http_status = status.HTTP_422_UNPROCESSABLE_CONTENT
    elif isinstance(exc, ConflictError):
        http_status = status.HTTP_409_CONFLICT
    elif isinstance(exc, BusinessRuleError):
        # quota_exceeded is a billing gate, not a generic bad request.
        if exc.code == "quota_exceeded":
            http_status = status.HTTP_402_PAYMENT_REQUIRED
        else:
            http_status = status.HTTP_400_BAD_REQUEST
    elif isinstance(exc, ExternalServiceError):
        http_status = status.HTTP_503_SERVICE_UNAVAILABLE
    else:
        http_status = status.HTTP_500_INTERNAL_SERVER_ERROR

    # Log at WARNING for 4xx, ERROR for 5xx
    log_method = logger.warning if http_status < 500 else logger.error
    log_method(
        "domain_error",
        error_code=exc.code,
        error_message=exc.message,
        error_type=type(exc).__name__,
        status_code=http_status,
        **exc.context,
    )

    return JSONResponse(status_code=http_status, content=_envelope(exc.to_dict()))


async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    """Handle FastAPI's HTTPException (raised by us or by FastAPI internals)."""
    return JSONResponse(
        status_code=exc.status_code,
        content=_envelope({
            "code": "http_error",
            "message": str(exc.detail),
            "context": {},
        }),
        headers=exc.headers,
    )


async def validation_exception_handler(
    _: Request, exc: RequestValidationError
) -> JSONResponse:
    """Pydantic input validation errors → 422 with structured field errors."""
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content=_envelope({
            "code": "validation_error",
            "message": "Request validation failed.",
            "context": {"errors": exc.errors()},
        }),
    )


async def unhandled_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    """Catch-all for unexpected exceptions.

    The detail is generic on purpose — we don't want internal error messages
    leaking to clients. Full details are in logs (with correlation_id).
    """
    logger.exception(
        "unhandled_exception",
        error_type=type(exc).__name__,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=_envelope({
            "code": "internal_error",
            "message": "An internal error occurred.",
            "context": {},
        }),
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def install_exception_handlers(app: FastAPI) -> None:
    """Register all exception handlers on the app.

    Order matters: more specific types must be registered first. FastAPI
    walks the registered handlers in order and uses the first match by
    isinstance check.
    """
    app.add_exception_handler(TrendStormError, domain_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(HTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)
