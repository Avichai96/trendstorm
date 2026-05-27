# TrendStorm Python SDK

The official Python SDK for [TrendStorm AI](https://trendstorm.io) — an autonomous multi-agent trend intelligence platform.

## Installation

```bash
pip install trendstorm
```

Requires Python 3.12+.

## Quick start

```python
import asyncio
from trendstorm_sdk import TrendStormClient

async def main():
    async with TrendStormClient(api_key="ts_live_...") as ts:
        # Create a category and register sources
        category = await ts.categories.create(
            name="AI Safety Research",
            keywords=["alignment", "interpretability"],
        )
        source = await ts.sources.add(
            category_id=category.id,
            url="https://arxiv.org/rss/cs.AI",
        )

        # Submit a job
        job = await ts.jobs.create(
            category_id=category.id,
            source_ids=[source.id],
        )

        # Stream real-time events
        async for event in ts.jobs.stream(job.job_id):
            print(f"[{event.seq}] {event.event_type.value}")
            if event.event_type.is_terminal:
                break

asyncio.run(main())
```

## What's included

| Resource | Description |
|---|---|
| `ts.categories` | Create and manage trend categories |
| `ts.sources` | Register RSS feeds, URLs, APIs |
| `ts.jobs` | Submit analysis jobs, poll status, stream events |
| `ts.reviews` | HITL review queue (approve / reject / refine) |
| `ts.quota` | Check monthly spend and limits |
| `ts.api_keys` | Manage API keys |

## Sync usage

For scripts and CLI tools that don't run an event loop:

```python
from trendstorm_sdk import SyncTrendStormClient

with SyncTrendStormClient(api_key="ts_live_...") as ts:
    cats = ts.categories.list()
    print(cats.categories)
```

!!! note
    SSE streaming (`ts.jobs.stream()`) is only available on the async client.
