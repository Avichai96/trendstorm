"""Quickstart — submit a job, stream events, retrieve the report.

Usage:
    export TRENDSTORM_API_KEY=ts_live_...
    export TRENDSTORM_BASE_URL=http://localhost:8080  # local dev
    python examples/quickstart.py
"""
from __future__ import annotations

import asyncio
import os

from trendstorm_sdk import TrendStormClient
from trendstorm_shared.types import StreamEventType


async def main() -> None:
    api_key = os.environ["TRENDSTORM_API_KEY"]
    base_url = os.environ.get("TRENDSTORM_BASE_URL", "https://api.trendstorm.io")

    async with TrendStormClient(api_key=api_key, base_url=base_url) as ts:
        # 1. Create a category.
        print("Creating category...")
        category = await ts.categories.create(
            name="AI Safety Research",
            keywords=["alignment", "interpretability", "RLHF", "AGI"],
        )
        print(f"  Category: {category.id} — {category.name}")

        # 2. Register sources.
        print("Registering sources...")
        from trendstorm_shared.types import SourceType
        source = await ts.sources.add(
            category_id=category.id,
            url="https://arxiv.org/rss/cs.AI",
            label="arXiv CS.AI",
            type=SourceType.RSS,
        )
        print(f"  Source: {source.id} — {source.url}")

        # 3. Submit a job.
        print("Submitting job...")
        accepted = await ts.jobs.create(
            category_id=category.id,
            source_ids=[source.id],
            note="Weekly AI safety digest",
        )
        print(f"  Job: {accepted.job_id} ({accepted.status})")
        print(f"  Stream URL: {base_url}{accepted.stream_url}")

        # 4. Stream events.
        print("\nStreaming events...")
        last_seq: int | None = None
        async for event in ts.jobs.stream(accepted.job_id):
            last_seq = event.seq
            icon = "✓" if event.event_type == StreamEventType.STAGE_COMPLETED else "·"
            print(f"  [{event.seq:04d}] {icon} {event.event_type.value} {event.payload}")
            if event.event_type.is_terminal:
                break

        # 5. Fetch final job state.
        job = await ts.jobs.get(accepted.job_id)
        print(f"\nFinal status: {job.status}")
        if job.report_id:
            print(f"Report ID:   {job.report_id}")
        if job.metrics:
            m = job.metrics
            print(f"Metrics:     {m.documents_ingested} docs, {m.chunks_created} chunks, "
                  f"{m.llm_input_tokens}+{m.llm_output_tokens} tokens")


if __name__ == "__main__":
    asyncio.run(main())
