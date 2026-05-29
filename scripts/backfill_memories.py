"""Backfill long-term memories for existing completed analyses.

For each Analysis in the COMPLETED state (across all tenants, or filtered by
--tenant-id / --category-id), this script:
    1. Checks whether an episodic memory already exists for this job (idempotent).
    2. Publishes a MemoryPendingEvent to trendstorm.memory.pending.v1.
    3. The memory-consolidation-worker picks it up and does the actual extraction.

The script does NOT do extraction inline — it's a fan-out publisher. This means
    - Safe to run against a live system (no double-writes).
    - Workers process at their own pace with full retry topology.
    - Can be interrupted and re-run: existing memories are skipped.

Usage:
    python scripts/backfill_memories.py
    python scripts/backfill_memories.py --tenant-id <id>
    python scripts/backfill_memories.py --tenant-id <id> --category-id <id>
    python scripts/backfill_memories.py --dry-run     # preview only, no publish

Environment:
    Reads settings from .env / .env.local (same as the API).
    Needs KAFKA__BOOTSTRAP_SERVERS and MONGO__URI to be correct.

Run from the repo root:
    uv run python scripts/backfill_memories.py
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Allow imports from src/ without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from trendstorm.infrastructure.kafka.producer import KafkaProducerClient
from trendstorm.infrastructure.mongo.client import MongoClient
from trendstorm.infrastructure.mongo.repositories import (
    MongoAnalysisRepository,
    MongoMemoryRepository,
)
from trendstorm.orchestration.events import MemoryPendingEvent
from trendstorm.orchestration.topics import Topic
from trendstorm.shared.config import get_settings
from trendstorm.shared.ids import new_id
from trendstorm.shared.logging import configure_logging, get_logger

logger = get_logger(__name__)


async def backfill(
    *,
    tenant_id: str | None,
    category_id: str | None,
    dry_run: bool,
) -> None:
    settings = get_settings()
    configure_logging(settings.app)

    mongo = MongoClient(settings.mongo)
    await mongo.connect()

    producer = KafkaProducerClient(settings.kafka)
    await producer.start()

    analysis_repo = MongoAnalysisRepository(mongo)
    memory_repo = MongoMemoryRepository(mongo)

    published = 0
    skipped = 0

    try:
        # Stream all completed analyses — cross-tenant if no filter given.
        async for analysis in analysis_repo.iter_completed(
            tenant_id=tenant_id,
            category_id=category_id,
        ):
            # Skip if episodic memory already exists.
            if await memory_repo.exists_for_job(analysis.tenant_id, analysis.job_id):
                logger.debug(
                    "backfill.skip.already_has_memory",
                    job_id=analysis.job_id,
                    analysis_id=analysis.id,
                )
                skipped += 1
                continue

            event = MemoryPendingEvent(
                event_id=new_id(),
                correlation_id=analysis.job_id,
                tenant_id=analysis.tenant_id,
                job_id=analysis.job_id,
                analysis_id=analysis.id,
                category_id=analysis.category_id,
                attempt=1,
            )

            if dry_run:
                logger.info(
                    "backfill.dry_run",
                    job_id=analysis.job_id,
                    tenant_id=analysis.tenant_id,
                    analysis_id=analysis.id,
                )
            else:
                await producer.send_and_wait(
                    Topic.MEMORY_PENDING.value,
                    value=event.model_dump_json().encode(),
                    key=analysis.job_id.encode(),
                )
                logger.info(
                    "backfill.published",
                    job_id=analysis.job_id,
                    tenant_id=analysis.tenant_id,
                    analysis_id=analysis.id,
                )
            published += 1

    finally:
        await producer.stop()
        await mongo.close()

    logger.info(
        "backfill.done",
        published=published,
        skipped=skipped,
        dry_run=dry_run,
    )
    print(
        f"Backfill {'(dry run) ' if dry_run else ''}complete: "
        f"{published} published, {skipped} skipped (already have memory)."
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tenant-id", help="Limit to a single tenant")
    parser.add_argument("--category-id", help="Limit to a single category (requires --tenant-id)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without publishing")
    args = parser.parse_args()
    if args.category_id and not args.tenant_id:
        parser.error("--category-id requires --tenant-id")
    return args


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(
        backfill(
            tenant_id=args.tenant_id,
            category_id=args.category_id,
            dry_run=args.dry_run,
        )
    )
