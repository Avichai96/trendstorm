#!/usr/bin/env python3
"""Idempotently apply all MongoDB indexes for TrendStorm.

Reads index definitions from `trendstorm.infrastructure.mongo.indexes.INDEXES`
and applies them. Re-running is safe — Mongo's `createIndex` is a no-op
when the index already exists with identical options.

Usage:
    uv run python scripts/seed_mongo_indexes.py

Run AFTER `make up` (Mongo must be running) and BEFORE serving traffic.
Suitable as a CI/CD step or a K8s Job that runs once per release.

Exit codes:
    0   all indexes created or already existed
    1   one or more indexes failed (see logs)
"""
from __future__ import annotations

import asyncio
import sys

from pymongo.errors import OperationFailure

from trendstorm.infrastructure.mongo.client import MongoClient
from trendstorm.infrastructure.mongo.indexes import INDEXES
from trendstorm.shared.config import get_settings
from trendstorm.shared.logging import configure_logging, get_logger


logger = get_logger(__name__)


async def main() -> int:
    configure_logging()
    settings = get_settings()
    mongo = MongoClient(settings.mongo)

    try:
        await mongo.connect()
    except Exception as e:  # noqa: BLE001
        logger.error("mongo_connect_failed", error=str(e))
        return 1

    failures = 0
    for spec in INDEXES:
        coll = mongo.db[spec.collection.value]
        try:
            applied_name = await coll.create_index(spec.keys, **spec.to_pymongo_kwargs())
            logger.info(
                "index_applied",
                collection=spec.collection.value,
                name=applied_name,
                keys=spec.keys,
            )
        except OperationFailure as e:
            # Most common cause: an index with the same name exists with
            # different options. Surface this — never silently overwrite.
            logger.error(
                "index_apply_failed",
                collection=spec.collection.value,
                name=spec.name,
                keys=spec.keys,
                error=str(e),
            )
            failures += 1

    await mongo.close()
    if failures:
        logger.error("seed_completed_with_failures", failures=failures)
        return 1

    logger.info("seed_complete", total=len(INDEXES))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
