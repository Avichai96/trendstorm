"""Jobs resource — create and monitor trend analysis jobs."""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from trendstorm_shared.models import (
    JobAcceptedResponse,
    JobListResponse,
    JobResponse,
    StreamEvent,
)
from trendstorm_shared.types import JobStatus

from .._sse import SSEStream
from ._base import AsyncAPIResource

if TYPE_CHECKING:
    pass


class JobsResource(AsyncAPIResource):
    """Submit and track trend analysis jobs.

    Examples::

        # Submit a job and stream results
        accepted = await ts.jobs.create(category_id=cat.id, source_ids=[src.id])
        async for event in ts.jobs.stream(accepted.job_id):
            print(event.event_type, event.payload)

        # Poll for status
        job = await ts.jobs.get(accepted.job_id)
        if job.status.is_terminal:
            print("done:", job.status)
    """

    async def create(
        self,
        *,
        category_id: str,
        source_ids: list[str] | None = None,
        note: str | None = None,
    ) -> JobAcceptedResponse:
        """Submit a new trend analysis job.

        Returns immediately (202 Accepted) with the job ID and SSE stream URL.
        Processing happens asynchronously in the worker pipeline.
        """
        body: dict = {"category_id": category_id}
        if source_ids:
            body["source_ids"] = source_ids
        if note is not None:
            body["note"] = note
        data = await self._post("/v1/jobs", body)
        return JobAcceptedResponse.model_validate(data)

    async def get(self, job_id: str) -> JobResponse:
        """Fetch the current state of a job."""
        data = await self._get(f"/v1/jobs/{job_id}")
        return JobResponse.model_validate(data)

    async def list(
        self,
        *,
        status: JobStatus | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> JobListResponse:
        """List jobs for the tenant, newest first, cursor-paginated."""
        data = await self._get(
            "/v1/jobs",
            status=status,
            limit=limit,
            cursor=cursor,
        )
        return JobListResponse.model_validate(data)

    def stream(
        self,
        job_id: str,
        *,
        last_event_id: int | None = None,
        heartbeat_timeout: float = 30.0,
        max_reconnects: int = 3,
    ) -> AsyncIterator[StreamEvent]:
        """Stream real-time events for a job as typed ``StreamEvent`` objects.

        The iterator yields until a terminal event (REPORT_READY, JOB_FAILED,
        JOB_REJECTED) is received, then closes automatically.

        Use ``last_event_id`` to resume a broken stream from where it left off::

            last_id: int | None = None
            async for event in ts.jobs.stream(job_id):
                last_id = event.seq
                process(event)

            # Later, if the stream drops:
            async for event in ts.jobs.stream(job_id, last_event_id=last_id):
                ...

        Args:
            job_id:            The job ID to stream.
            last_event_id:     Resume from this seq number (``Last-Event-ID``).
            heartbeat_timeout: Seconds of silence before raising HeartbeatTimeout.
            max_reconnects:    Automatic reconnects on transient connection drops.
        """
        client = self._client._http_client()
        auth_headers = self._client._auth_headers()
        url = f"/v1/jobs/{job_id}/stream"
        return SSEStream(
            client,
            url,
            auth_headers,
            last_event_id=last_event_id,
            heartbeat_timeout=heartbeat_timeout,
            max_reconnects=max_reconnects,
        )

    def resume(
        self,
        job_id: str,
        *,
        last_event_id: int,
        heartbeat_timeout: float = 30.0,
    ) -> AsyncIterator[StreamEvent]:
        """Resume a stream from a specific seq number.

        Convenience alias for ``stream(job_id, last_event_id=last_event_id)``.
        """
        return self.stream(job_id, last_event_id=last_event_id, heartbeat_timeout=heartbeat_timeout)
