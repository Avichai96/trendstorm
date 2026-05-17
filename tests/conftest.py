"""Shared pytest fixtures."""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure tests don't pick up the developer's .env file.

    We clear Settings-related env vars and tell pydantic-settings to skip
    .env loading. Each test that needs config sets its own.
    """
    # Block .env from being read
    monkeypatch.setenv("_TRENDSTORM_DISABLE_DOTENV", "1")

    # Clear all double-underscore env vars from current shell
    for key in list(os.environ):
        if "__" in key and (
            key.startswith(("APP__", "MONGO__", "KAFKA__", "REDIS__",
                            "VECTOR__", "BLOB__", "LLM__", "OTEL__",
                            "LANGSMITH__", "API__"))
        ):
            monkeypatch.delenv(key, raising=False)
