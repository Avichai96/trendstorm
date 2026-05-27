"""Integration test configuration.

Integration tests require a live TrendStorm API.
Run with: TRENDSTORM_BASE_URL=http://localhost:8080 TRENDSTORM_API_KEY=ts_test_... pytest -m integration

The API must be running with a real database. Use `make up && make up-app` to start
the full stack locally.
"""
from __future__ import annotations

import os

import pytest

from trendstorm_sdk import TrendStormClient


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: requires live TrendStorm API")
    config.addinivalue_line("markers", "staging: requires staging env; CI only")


@pytest.fixture
def base_url() -> str:
    return os.environ.get("TRENDSTORM_BASE_URL", "http://localhost:8080")


@pytest.fixture
def api_key() -> str:
    key = os.environ.get("TRENDSTORM_API_KEY")
    if not key:
        pytest.skip("TRENDSTORM_API_KEY not set — skipping integration test")
    return key


@pytest.fixture
async def ts(base_url: str, api_key: str) -> TrendStormClient:
    async with TrendStormClient(api_key=api_key, base_url=base_url) as client:
        yield client
