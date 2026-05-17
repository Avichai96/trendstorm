"""Outbox entry domain model.

The outbox pattern closes the persist-before-publish window in job creation.
JobService wraps (jobs.insert + outbox.insert) in a single Mongo transaction.
The outbox-relay worker polls pending entries and publishes to Kafka; the
existing Kafka idempotency layer handles any double-publish.

The entry carries a raw `payload` dict (pre-serialized event body) and the
Kafka topic + key so the relay worker needs no domain knowledge of individual
event types.
"""
from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from trendstorm.shared.ids import new_id


class OutboxEntry(BaseModel):
    """A pending Kafka publish, persisted atomically with the triggering write."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    tenant_id: str

    # Kafka routing
    topic: str
    key: str                        # Kafka message key (usually job_id)
    payload: dict[str, object]      # Pre-serialized event body (JSON-ready)

    # Lifecycle
    retry_count: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    published_at: datetime | None = None  # None = pending
