"""Domain exceptions.

Hierarchy:
    TrendStormError                  base for all domain errors
    ├── ConfigError                  invalid configuration
    ├── ValidationError              input validation failure
    ├── NotFoundError                resource missing
    ├── ConflictError                concurrent modification, dup key
    ├── ExternalServiceError         downstream system failed
    │   ├── LLMError                 LLM provider failure
    │   │   ├── LLMTransientError    retryable (rate limit, timeout, 5xx)
    │   │   │   ├── LLMRateLimitError
    │   │   │   └── LLMTimeoutError
    │   │   ├── LLMPermanentError    not retryable (bad auth, malformed request)
    │   │   └── LLMSchemaError       structured output parse failed
    │   ├── DatabaseError            Mongo/Redis failure
    │   └── BrokerError              Kafka failure
    └── BusinessRuleError            policy violation

Why a hierarchy?
    - HTTP layer can map exception types to status codes:
        NotFoundError       -> 404
        ValidationError     -> 422
        ConflictError       -> 409
        ExternalServiceError-> 503
        TrendStormError     -> 500
    - Catch blocks can be specific without listing every concrete type.
    - Domain code raises domain exceptions; infrastructure code raises
      infra exceptions; the API layer maps both to HTTP.

Each error carries:
    - `message`: human-readable
    - `code`: machine-readable identifier (snake_case) — stable contract
    - `context`: structured data for logs/clients
"""
from __future__ import annotations

from typing import Any


class TrendStormError(Exception):
    """Base class for all domain errors.

    Concrete subclasses should set a class-level `default_code` and override
    `__init__` only if they need to take extra args.
    """

    default_code: str = "internal_error"
    default_message: str = "An internal error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.default_message
        self.code = code or self.default_code
        self.context = context or {}
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        """Serializable representation for API responses and logs."""
        return {
            "code": self.code,
            "message": self.message,
            "context": self.context,
        }


class ConfigError(TrendStormError):
    default_code = "config_error"
    default_message = "Invalid configuration."


class ValidationError(TrendStormError):
    default_code = "validation_error"
    default_message = "Input validation failed."


class NotFoundError(TrendStormError):
    default_code = "not_found"
    default_message = "Resource not found."


class ConflictError(TrendStormError):
    default_code = "conflict"
    default_message = "Resource conflict."


class BusinessRuleError(TrendStormError):
    default_code = "business_rule_violation"
    default_message = "Operation violates a business rule."


# --- External service errors -------------------------------------------------

class ExternalServiceError(TrendStormError):
    default_code = "external_service_error"
    default_message = "An external service is unavailable."


class DatabaseError(ExternalServiceError):
    default_code = "database_error"
    default_message = "Database operation failed."


class BrokerError(ExternalServiceError):
    default_code = "broker_error"
    default_message = "Message broker operation failed."


class LLMError(ExternalServiceError):
    default_code = "llm_error"
    default_message = "LLM provider error."


class LLMTransientError(LLMError):
    """Retryable LLM errors: rate limits, timeouts, transient 5xx.

    The retry wrapper in infrastructure/llm/retry.py catches this type;
    it must NOT catch LLMPermanentError or LLMSchemaError.
    """

    default_code = "llm_transient_error"
    default_message = "Transient LLM provider error; retry is safe."


class LLMRateLimitError(LLMTransientError):
    default_code = "llm_rate_limit"
    default_message = "LLM provider rate limit exceeded."


class LLMTimeoutError(LLMTransientError):
    default_code = "llm_timeout"
    default_message = "LLM provider timed out."


class LLMPermanentError(LLMError):
    """Non-retryable LLM errors: bad auth, malformed request, quota exhausted."""

    default_code = "llm_permanent_error"
    default_message = "Permanent LLM provider error; do not retry."


class LLMSchemaError(LLMError):
    """LLM returned a response that could not be parsed as the expected schema.

    Separate from LLMPermanentError because the LLM itself worked fine;
    the output was just malformed. Callers may want to retry with a different
    prompt rather than surfacing a provider error.
    """

    default_code = "llm_schema_error"
    default_message = "LLM output did not match the expected schema."


class FetchError(ExternalServiceError):
    default_code = "fetch_error"
    default_message = "HTTP fetch failed."


class HostRateLimitedError(FetchError):
    """Our own token-bucket denied the request before it was even sent."""

    default_code = "host_rate_limited"
    default_message = "Per-host rate limit exceeded; back off and retry."


class SSRFBlockedError(FetchError):
    """URL rejected by the SSRF validator before any network connection was made.

    `reason` is a machine-readable key used as a Prometheus label — must match
    an entry in SecurityBlockReason enum in shared/metrics/registry.py.
    """

    default_code = "ssrf_blocked"
    default_message = "URL blocked by SSRF validator."

    def __init__(
        self,
        message: str | None = None,
        *,
        reason: str,
        url: str,
        context: dict | None = None,
    ) -> None:
        super().__init__(
            message or self.default_message,
            context={"reason": reason, "url": url, **(context or {})},
        )
        self.reason = reason
        self.url = url


class ParseError(ExternalServiceError):
    default_code = "parse_error"
    default_message = "Content could not be parsed."


class BlobError(ExternalServiceError):
    default_code = "blob_error"
    default_message = "Blob storage operation failed."
