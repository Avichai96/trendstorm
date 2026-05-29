"""Synchronous wrapper for TrendStormClient.

``SyncTrendStormClient`` wraps the async client so callers that can't use
``asyncio`` (scripts, notebooks, Celery workers in sync context) still get a
first-class API without wrestling with event loops themselves.

Implementation note: each method call creates a new event loop via
``asyncio.run()``. This is safe and correct for low-frequency usage (scripts,
one-shot CLI calls). For high-throughput scenarios, use the async client
directly — multiple ``asyncio.run()`` calls per second have measurable
overhead from loop creation and teardown.

Stream events are NOT available on the sync client because generators over
async iterators cannot be trivially bridged. Use the async client for SSE.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _SyncResource:
    """Mixin that delegates sync calls to an async resource via asyncio.run()."""

    def __init__(self, async_resource: Any) -> None:
        self._async = async_resource

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._async, name)
        if asyncio.iscoroutinefunction(attr):

            def _sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                return _run(attr(*args, **kwargs))

            _sync_wrapper.__name__ = name
            return _sync_wrapper
        return attr


class SyncTrendStormClient:
    """Synchronous facade over ``TrendStormClient``.

    Use as a context manager::

        from trendstorm_sdk import SyncTrendStormClient

        with SyncTrendStormClient(api_key="ts_live_...") as ts:
            cats = ts.categories.list()
            job = ts.jobs.create(category_id="...", source_ids=["..."])

    Note: SSE streaming (``ts.jobs.stream()``) is NOT available on the sync
    client. Use ``TrendStormClient`` (async) for real-time event streams.
    """

    def __init__(self, **kwargs: Any) -> None:
        from ._client import TrendStormClient

        self._async_client = TrendStormClient(**kwargs)
        self._entered = False

    def __enter__(self) -> "SyncTrendStormClient":
        self._async_client = _run(self._async_client.__aenter__())
        self._entered = True
        return self

    def __exit__(self, *args: Any) -> None:
        _run(self._async_client.__aexit__(*args))
        self._entered = False

    @property
    def categories(self) -> _SyncResource:
        return _SyncResource(self._async_client.categories)

    @property
    def sources(self) -> _SyncResource:
        return _SyncResource(self._async_client.sources)

    @property
    def jobs(self) -> _SyncResource:
        return _SyncResource(self._async_client.jobs)

    @property
    def reviews(self) -> _SyncResource:
        return _SyncResource(self._async_client.reviews)

    @property
    def quota(self) -> _SyncResource:
        return _SyncResource(self._async_client.quota)

    @property
    def api_keys(self) -> _SyncResource:
        return _SyncResource(self._async_client.api_keys)
