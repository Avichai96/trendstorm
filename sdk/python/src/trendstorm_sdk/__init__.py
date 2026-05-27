"""TrendStorm AI Python SDK.

Quick start::

    import asyncio
    from trendstorm_sdk import TrendStormClient

    async def main():
        async with TrendStormClient(api_key="ts_live_...") as ts:
            cat = await ts.categories.create(name="AI Safety")
            job = await ts.jobs.create(category_id=cat.id)
            async for event in ts.jobs.stream(job.job_id):
                print(event.event_type, event.payload)
                if event.event_type.is_terminal:
                    break

    asyncio.run(main())

Sync usage::

    from trendstorm_sdk import SyncTrendStormClient

    with SyncTrendStormClient(api_key="ts_live_...") as ts:
        cats = ts.categories.list()
"""
from trendstorm_sdk._client import TrendStormClient
from trendstorm_sdk._errors import (
    APIError,
    ConfigurationError,
    HeartbeatTimeout,
    NotFound,
    RateLimited,
    ServerError,
    StreamError,
    TrendStormError,
    Unauthorized,
    ValidationError,
)
from trendstorm_sdk._sync import SyncTrendStormClient

__version__ = "0.1.0"

__all__ = [
    "TrendStormClient",
    "SyncTrendStormClient",
    # Errors
    "TrendStormError",
    "ConfigurationError",
    "StreamError",
    "HeartbeatTimeout",
    "APIError",
    "RateLimited",
    "NotFound",
    "Unauthorized",
    "ValidationError",
    "ServerError",
]
