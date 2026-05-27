# TrendStorm Python SDK

[![PyPI](https://img.shields.io/pypi/v/trendstorm)](https://pypi.org/project/trendstorm/)
[![Python](https://img.shields.io/pypi/pyversions/trendstorm)](https://pypi.org/project/trendstorm/)

Python SDK for the [TrendStorm AI](https://trendstorm.io) trend intelligence platform.

## Install

```bash
pip install trendstorm
```

## Quick start

```python
import asyncio
from trendstorm_sdk import TrendStormClient

async def main():
    async with TrendStormClient(api_key="ts_live_...") as ts:
        cat = await ts.categories.create(name="AI Safety Research")
        job = await ts.jobs.create(category_id=cat.id)
        async for event in ts.jobs.stream(job.job_id):
            print(event.event_type.value, event.payload)
            if event.event_type.is_terminal:
                break

asyncio.run(main())
```

## Sync usage

```python
from trendstorm_sdk import SyncTrendStormClient

with SyncTrendStormClient(api_key="ts_live_...") as ts:
    cats = ts.categories.list()
```

## Features

- **Full API coverage** — categories, sources, jobs, reviews, quota, API keys
- **Real-time SSE streaming** — typed `StreamEvent` objects with Last-Event-ID resumption
- **Automatic retry** — exponential backoff for 429 / 5xx; honours `Retry-After` header
- **HITL reviews** — approve, reject, or request refinement of analyses
- **OAuth 2.0** — bearer token with auto-refresh
- **Sync wrapper** — `SyncTrendStormClient` for scripts and CLI tools

## Documentation

Full documentation at [docs.trendstorm.io/sdk/python](https://docs.trendstorm.io/sdk/python).

## License

MIT
