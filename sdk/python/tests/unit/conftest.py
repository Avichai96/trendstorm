"""Shared fixtures for SDK unit tests.

All tests in this directory are pure (no network I/O, no Docker).
respx is used to mock httpx requests.
"""
from __future__ import annotations

import pytest
import respx

from trendstorm_sdk import TrendStormClient


@pytest.fixture
def base_url() -> str:
    return "https://api.trendstorm.test"


@pytest.fixture
def api_key() -> str:
    return "ts_test_UnitTestApiKey1234567890"


@pytest.fixture
async def client(base_url: str, api_key: str) -> TrendStormClient:
    async with TrendStormClient(api_key=api_key, base_url=base_url, max_retries=0) as ts:
        yield ts
