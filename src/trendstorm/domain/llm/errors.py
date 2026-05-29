"""LLM error types — single import location for provider and domain code.

Re-exports all LLM-related errors from shared.errors. The retry wrapper
(infrastructure/llm/retry.py) catches LLMTransientError; it must NOT
catch LLMPermanentError or LLMSchemaError.
"""

from __future__ import annotations

from trendstorm.shared.errors import (
    LLMError,
    LLMPermanentError,
    LLMRateLimitError,
    LLMSchemaError,
    LLMTimeoutError,
    LLMTransientError,
)

__all__ = [
    "LLMError",
    "LLMPermanentError",
    "LLMRateLimitError",
    "LLMSchemaError",
    "LLMTimeoutError",
    "LLMTransientError",
]
