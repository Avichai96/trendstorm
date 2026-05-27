"""Unit tests for the error hierarchy."""
from __future__ import annotations

import pytest

from trendstorm_sdk._errors import (
    APIError,
    NotFound,
    RateLimited,
    ServerError,
    TrendStormError,
    Unauthorized,
    ValidationError,
)


@pytest.mark.unit
class TestErrorHierarchy:
    def test_all_api_errors_are_trendstorm_errors(self) -> None:
        for klass in (NotFound, RateLimited, Unauthorized, ValidationError, ServerError):
            assert issubclass(klass, TrendStormError)
            assert issubclass(klass, APIError)

    def test_api_error_str_contains_code_and_message(self) -> None:
        err = APIError(status_code=400, error_code="bad_request", message="missing field")
        assert "400" in str(err)
        assert "bad_request" in str(err)
        assert "missing field" in str(err)

    def test_from_response_maps_404_to_not_found(self) -> None:
        body = {"error": {"code": "not_found", "message": "Job not found"}, "correlation_id": "abc"}
        err = APIError.from_response(404, body, {})
        assert isinstance(err, NotFound)
        assert err.error_code == "not_found"
        assert err.correlation_id == "abc"

    def test_from_response_maps_429_to_rate_limited(self) -> None:
        body = {"error": {"code": "rate_limited", "message": "Too many requests"}}
        err = APIError.from_response(429, body, {"x-request-id": "req-123"})
        assert isinstance(err, RateLimited)
        assert err.request_id == "req-123"

    def test_from_response_maps_401_to_unauthorized(self) -> None:
        body = {"error": {"code": "unauthorized", "message": "Bad key"}}
        err = APIError.from_response(401, body, {})
        assert isinstance(err, Unauthorized)

    def test_from_response_maps_403_to_unauthorized(self) -> None:
        body = {"error": {"code": "forbidden", "message": "Missing role"}}
        err = APIError.from_response(403, body, {})
        assert isinstance(err, Unauthorized)

    def test_from_response_maps_500_to_server_error(self) -> None:
        body = {"error": {"code": "internal", "message": "crash"}}
        err = APIError.from_response(500, body, {})
        assert isinstance(err, ServerError)

    def test_from_response_unknown_status_is_plain_api_error(self) -> None:
        body = {"error": {"code": "teapot", "message": "I am a teapot"}}
        err = APIError.from_response(418, body, {})
        assert type(err) is APIError

    def test_raw_body_stored(self) -> None:
        body = {"error": {"code": "x", "message": "y"}, "extra": "data"}
        err = APIError.from_response(400, body, {})
        assert err.raw == body
