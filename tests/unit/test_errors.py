"""Unit tests for the domain exception hierarchy."""
from __future__ import annotations

import pytest

from trendstorm.shared.errors import (
    ConflictError,
    DatabaseError,
    ExternalServiceError,
    LLMError,
    LLMPermanentError,
    LLMRateLimitError,
    LLMSchemaError,
    LLMTimeoutError,
    LLMTransientError,
    NotFoundError,
    TrendStormError,
    ValidationError,
)


@pytest.mark.unit
class TestErrorHierarchy:
    def test_all_inherit_from_base(self) -> None:
        for cls in [NotFoundError, ValidationError, ConflictError,
                    ExternalServiceError, DatabaseError, LLMError]:
            assert issubclass(cls, TrendStormError)

    def test_database_is_external(self) -> None:
        assert issubclass(DatabaseError, ExternalServiceError)

    def test_rate_limit_is_llm_error(self) -> None:
        assert issubclass(LLMRateLimitError, LLMError)

    def test_transient_is_llm_error(self) -> None:
        assert issubclass(LLMTransientError, LLMError)

    def test_rate_limit_is_transient(self) -> None:
        assert issubclass(LLMRateLimitError, LLMTransientError)

    def test_timeout_is_transient(self) -> None:
        assert issubclass(LLMTimeoutError, LLMTransientError)

    def test_permanent_is_llm_error(self) -> None:
        assert issubclass(LLMPermanentError, LLMError)

    def test_schema_is_llm_error(self) -> None:
        assert issubclass(LLMSchemaError, LLMError)

    def test_schema_not_transient(self) -> None:
        # LLMSchemaError is a content failure, not a provider availability failure.
        assert not issubclass(LLMSchemaError, LLMTransientError)

    def test_transient_caught_broadly(self) -> None:
        err = LLMRateLimitError()
        assert isinstance(err, LLMError)
        assert isinstance(err, LLMTransientError)

    def test_schema_error_code(self) -> None:
        err = LLMSchemaError()
        assert err.code == "llm_schema_error"

    def test_permanent_error_code(self) -> None:
        err = LLMPermanentError()
        assert err.code == "llm_permanent_error"

    def test_default_code_used(self) -> None:
        err = NotFoundError()
        assert err.code == "not_found"
        assert err.message == "Resource not found."

    def test_explicit_overrides_default(self) -> None:
        err = NotFoundError("Custom message", code="custom_code", context={"id": "abc"})
        assert err.code == "custom_code"
        assert err.message == "Custom message"
        assert err.context == {"id": "abc"}

    def test_to_dict_shape(self) -> None:
        err = ValidationError("bad", context={"field": "name"})
        d = err.to_dict()
        assert d == {
            "code": "validation_error",
            "message": "bad",
            "context": {"field": "name"},
        }
