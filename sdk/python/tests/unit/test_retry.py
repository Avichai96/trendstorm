"""Unit tests for the retry / backoff logic."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from trendstorm_sdk._retry import _backoff, _parse_retry_after, retry_request


@pytest.mark.unit
class TestParseRetryAfter:
    def test_integer_seconds(self) -> None:
        resp = MagicMock(spec=httpx.Response)
        resp.headers = {"retry-after": "30"}
        assert _parse_retry_after(resp) == 30.0

    def test_float_seconds(self) -> None:
        resp = MagicMock(spec=httpx.Response)
        resp.headers = {"retry-after": "1.5"}
        assert _parse_retry_after(resp) == 1.5

    def test_http_date(self) -> None:
        import time
        future = time.time() + 60
        import email.utils
        date_str = email.utils.formatdate(future, usegmt=True)
        resp = MagicMock(spec=httpx.Response)
        resp.headers = {"retry-after": date_str}
        result = _parse_retry_after(resp)
        assert result is not None
        assert 55 < result < 65

    def test_missing_header_returns_none(self) -> None:
        resp = MagicMock(spec=httpx.Response)
        resp.headers = {}
        assert _parse_retry_after(resp) is None

    def test_garbage_header_returns_none(self) -> None:
        resp = MagicMock(spec=httpx.Response)
        resp.headers = {"retry-after": "not-a-date"}
        assert _parse_retry_after(resp) is None


@pytest.mark.unit
class TestBackoff:
    def test_exponential_growth(self) -> None:
        assert _backoff(0) == 1.0
        assert _backoff(1) == 2.0
        assert _backoff(2) == 4.0
        assert _backoff(3) == 8.0

    def test_cap_at_60(self) -> None:
        assert _backoff(10) == 60.0
        assert _backoff(100) == 60.0


@pytest.mark.unit
class TestRetryRequest:
    async def test_success_on_first_attempt(self) -> None:
        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        fn = AsyncMock(return_value=response)
        result = await retry_request(fn, max_retries=3)
        assert result is response
        fn.assert_called_once()

    async def test_retries_on_429(self) -> None:
        rate_limited = MagicMock(spec=httpx.Response)
        rate_limited.status_code = 429
        rate_limited.headers = {}
        rate_limited.url = "http://test"

        ok = MagicMock(spec=httpx.Response)
        ok.status_code = 200
        ok.url = "http://test"

        fn = AsyncMock(side_effect=[rate_limited, ok])
        with patch("trendstorm_sdk._retry.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await retry_request(fn, max_retries=3)
        assert result is ok
        assert fn.call_count == 2
        mock_sleep.assert_called_once()

    async def test_respects_retry_after_header(self) -> None:
        rate_limited = MagicMock(spec=httpx.Response)
        rate_limited.status_code = 429
        rate_limited.headers = {"retry-after": "42"}
        rate_limited.url = "http://test"

        ok = MagicMock(spec=httpx.Response)
        ok.status_code = 200

        fn = AsyncMock(side_effect=[rate_limited, ok])
        with patch("trendstorm_sdk._retry.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await retry_request(fn, max_retries=3)
        mock_sleep.assert_called_once_with(42.0)

    async def test_gives_up_after_max_retries(self) -> None:
        bad = MagicMock(spec=httpx.Response)
        bad.status_code = 503
        bad.headers = {}
        bad.url = "http://test"

        fn = AsyncMock(return_value=bad)
        with patch("trendstorm_sdk._retry.asyncio.sleep", new_callable=AsyncMock):
            result = await retry_request(fn, max_retries=2)
        assert result is bad
        assert fn.call_count == 3  # initial + 2 retries

    async def test_retries_on_connect_error(self) -> None:
        ok = MagicMock(spec=httpx.Response)
        ok.status_code = 200

        fn = AsyncMock(side_effect=[httpx.ConnectError("refused"), ok])
        with patch("trendstorm_sdk._retry.asyncio.sleep", new_callable=AsyncMock):
            result = await retry_request(fn, max_retries=3)
        assert result is ok

    async def test_raises_after_network_errors_exhausted(self) -> None:
        fn = AsyncMock(side_effect=httpx.ConnectError("refused"))
        with patch("trendstorm_sdk._retry.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(httpx.ConnectError):
                await retry_request(fn, max_retries=2)
        assert fn.call_count == 3
