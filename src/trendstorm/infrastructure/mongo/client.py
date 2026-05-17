"""MongoDB async client wrapper.

Owns the Motor (async pymongo) AsyncIOMotorClient lifecycle.
One instance per process; shared across all repositories.

Design:
    - `connect()` creates the client and verifies connectivity with a ping.
    - `close()` cleanly shuts down the connection pool.
    - `db` property returns the configured database.
    - `health_check()` is for readiness probes — fast, non-throwing.

Why a wrapper class instead of a module-level client?
    - Lifecycle: tied to FastAPI lifespan, not import time.
    - Testability: tests inject a mock client.
    - Multiple clients: future support for sharded reads, separate logs DB, etc.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo.errors import PyMongoError

from trendstorm.shared.config import MongoSettings
from trendstorm.shared.errors import DatabaseError
from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)


class MongoClient:
    """Async MongoDB client lifecycle manager."""

    def __init__(self, settings: MongoSettings) -> None:
        self._settings = settings
        self._client: AsyncIOMotorClient | None = None  # type: ignore[type-arg]  # motor stubs lack precise generic params

    async def connect(self) -> None:
        """Create the underlying client and verify connectivity.

        Idempotent: re-calling is a no-op.
        Raises DatabaseError if the initial connection fails.
        """
        if self._client is not None:
            return

        logger.info("mongo_connecting", database=self._settings.database)
        self._client = AsyncIOMotorClient(
            self._settings.uri.get_secret_value(),
            maxPoolSize=self._settings.max_pool_size,
            minPoolSize=self._settings.min_pool_size,
            serverSelectionTimeoutMS=self._settings.server_selection_timeout_ms,
            # uuidRepresentation must be set explicitly in newer pymongo
            uuidRepresentation="standard",
        )
        try:
            # Force the driver to actually connect & select a server.
            # `ping` is the canonical "are you there" command.
            await asyncio.wait_for(
                self._client.admin.command("ping"),
                timeout=self._settings.server_selection_timeout_ms / 1000 + 1,
            )
        except (PyMongoError, TimeoutError) as e:
            self._client = None
            raise DatabaseError(
                "Mongo connection failed during startup",
                context={"error": str(e), "error_type": type(e).__name__},
            ) from e
        logger.info("mongo_connected", database=self._settings.database)

    async def close(self) -> None:
        """Close the connection pool. Idempotent."""
        if self._client is None:
            return
        logger.info("mongo_closing")
        self._client.close()
        self._client = None

    @property
    def db(self) -> AsyncIOMotorDatabase:  # type: ignore[type-arg]  # motor stubs lack precise generic params
        """The configured database. Raises if not connected."""
        if self._client is None:
            raise DatabaseError("Mongo client not initialized; call connect() first")
        return self._client[self._settings.database]

    @property
    def client(self) -> AsyncIOMotorClient:  # type: ignore[type-arg]  # motor stubs lack precise generic params
        """Raw client for admin operations / transactions. Raises if not connected."""
        if self._client is None:
            raise DatabaseError("Mongo client not initialized; call connect() first")
        return self._client

    async def health_check(self) -> bool:
        """Fast non-throwing health check for readiness probes.

        Returns True if a ping succeeds within a tight deadline.
        """
        if self._client is None:
            return False
        try:
            await asyncio.wait_for(self._client.admin.command("ping"), timeout=2.0)
        except (PyMongoError, TimeoutError):
            return False
        return True
