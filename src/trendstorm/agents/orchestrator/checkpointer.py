"""MongoDB checkpointer for LangGraph.

What is a checkpointer?
    LangGraph serializes the JobState after every node transition and writes
    it to durable storage. If the worker crashes, the next consumer that
    picks up the Kafka message loads the latest checkpoint and resumes from
    there — instead of starting from scratch.

Why MongoDB?
    We already have it for jobs/sources/chunks. One fewer system to operate.
    LangGraph's MongoSaver uses transactions, which is why Phase 2 set up a
    replica set even for local dev.

Collections used (auto-created by `setup()`):
    - checkpoints           one row per (thread_id, checkpoint_id)
    - checkpoint_writes     pending writes (for write-then-checkpoint)

thread_id mapping:
    LangGraph identifies a workflow run by `thread_id` in its config.
    We use the job_id directly: `thread_id = job_id`. This means one job =
    one continuous thread of checkpoints in Mongo. Resuming a workflow is
    just: `await graph.ainvoke(state, config={"configurable":
    {"thread_id": job_id}})`.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import pymongo
from langgraph.checkpoint.mongodb import MongoDBSaver

from trendstorm.shared.config import MongoSettings
from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver


logger = get_logger(__name__)


class MongoCheckpointer:
    """Lifecycle-managed wrapper around LangGraph's MongoDBSaver.

    Owns a dedicated synchronous pymongo connection. LangGraph's MongoDBSaver
    wraps sync operations with run_in_executor to serve the async graph nodes.
    This connection is separate from the motor (async) client used elsewhere.
    """

    def __init__(self, settings: MongoSettings, *, db_name: str | None = None) -> None:
        self._settings = settings
        self._db_name = db_name
        self._saver: MongoDBSaver | None = None
        self._sync_client: pymongo.MongoClient | None = None  # type: ignore[type-arg]

    async def start(self) -> None:
        """Create the saver and its sync pymongo connection. Idempotent."""
        if self._saver is not None:
            return
        db_name = self._db_name or self._settings.database
        self._sync_client = pymongo.MongoClient(self._settings.uri.get_secret_value())
        self._saver = MongoDBSaver(client=self._sync_client, db_name=db_name)
        logger.info("checkpointer_started", db=db_name)

    async def close(self) -> None:
        """Close the saver and its sync pymongo connection."""
        if self._saver is not None:
            self._saver.close()
            self._saver = None
        self._sync_client = None

    @property
    def saver(self) -> BaseCheckpointSaver:  # type: ignore[type-arg]  # langgraph stubs
        if self._saver is None:
            raise RuntimeError("Checkpointer not started; call start() first")
        return self._saver
