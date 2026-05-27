# Quickstart

This guide takes you from zero to a complete trend analysis pipeline in under 5 minutes.

## Prerequisites

- Python 3.12+
- A TrendStorm API key (`ts_live_*` for production, `ts_test_*` for sandbox)

## Install

```bash
pip install trendstorm
```

## Set your API key

```bash
export TRENDSTORM_API_KEY="ts_live_..."
```

Or pass it directly to the constructor:

```python
from trendstorm_sdk import TrendStormClient
ts = TrendStormClient(api_key="ts_live_...")
```

## Full workflow

```python
import asyncio
from trendstorm_sdk import TrendStormClient
from trendstorm_shared.types import SourceType, StreamEventType

async def main():
    async with TrendStormClient() as ts:  # reads TRENDSTORM_API_KEY from env

        # 1. Create a category
        cat = await ts.categories.create(
            name="AI Safety Research",
            keywords=["alignment", "interpretability", "RLHF"],
        )
        print(f"Category: {cat.id}")

        # 2. Register data sources
        src = await ts.sources.add(
            category_id=cat.id,
            url="https://arxiv.org/rss/cs.AI",
            label="arXiv AI",
            type=SourceType.RSS,
        )

        # 3. Submit a job
        job = await ts.jobs.create(
            category_id=cat.id,
            source_ids=[src.id],
            note="Weekly digest",
        )
        print(f"Job {job.job_id} submitted — streaming...")

        # 4. Stream results live
        async for event in ts.jobs.stream(job.job_id):
            print(f"  [{event.seq:04d}] {event.event_type.value}")
            if event.event_type == StreamEventType.REPORT_READY:
                report_id = event.payload.get("report_id")
                print(f"  Report ready: {report_id}")
            if event.event_type.is_terminal:
                break

        # 5. Get final job state
        final = await ts.jobs.get(job.job_id)
        print(f"\nStatus: {final.status}")
        print(f"Tokens: {final.metrics.llm_input_tokens} in / {final.metrics.llm_output_tokens} out")

asyncio.run(main())
```

## Checking quota before submitting

```python
quota = await ts.quota.current_month()
if not quota.allowed:
    print(f"Quota exceeded: {quota.reason}")
else:
    job = await ts.jobs.create(...)
```

## Listing past jobs

```python
jobs = await ts.jobs.list(limit=10)
for job in jobs.jobs:
    print(job.id, job.status, job.created_at.date())

# Paginate
if jobs.next_cursor:
    page2 = await ts.jobs.list(cursor=jobs.next_cursor)
```
