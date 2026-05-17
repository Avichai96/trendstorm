"""JobRepository protocol.

This is a Python `Protocol` (structural type) — the contract that any
job persistence implementation must satisfy.

Why Protocol and not abstract base class?
    - Structural typing: implementations don't need to inherit. Anything
      with matching method signatures satisfies the protocol.
    - No runtime cost.
    - Cleaner duck typing — testing fakes don't need to inherit either.
    - Matches Python's idiom (PEP 544).

The protocol lives in `domain/` so services depend on it. The concrete
implementation lives in `infrastructure/mongo/repositories/` so it can
import Mongo. Services NEVER import the concrete class — only the protocol.
This is the hexagonal architecture seam.
"""
from __future__ import annotations

from typing import Protocol

from trendstorm.domain.jobs.models import Job
from trendstorm.shared.types import JobStatus


class JobRepository(Protocol):
    """Persistence contract for Job entities."""

    async def insert(self, job: Job, *, session: object | None = None) -> None:
        """Insert a new job. Raises ConflictError on duplicate ID.

        `session` is an opaque handle forwarded to the persistence layer
        when the caller needs to run this insert inside a transaction (e.g.
        the outbox pattern in JobService). Implementations that do not
        support transactions should accept and ignore it.
        """
        ...

    async def get(self, tenant_id: str, job_id: str) -> Job | None:
        """Fetch a job by tenant + id. Returns None if not found.

        Tenant scope is enforced HERE, not in callers. Every repository
        method takes tenant_id; the caller can't bypass tenant isolation.
        """
        ...

    async def update_status(
        self,
        tenant_id: str,
        job_id: str,
        status: JobStatus,
        *,
        failure_code: str | None = None,
        failure_message: str | None = None,
    ) -> None:
        """Atomically update job status with optional failure info.

        Sets `updated_at` and, if status is terminal, `completed_at`.
        """
        ...

    async def list_by_tenant(
        self,
        tenant_id: str,
        *,
        status: JobStatus | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[Job], str | None]:
        """List jobs for a tenant. Returns (jobs, next_cursor)."""
        ...
