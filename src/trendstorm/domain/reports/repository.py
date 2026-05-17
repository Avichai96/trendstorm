"""ReportRepository protocol."""
from __future__ import annotations

from typing import Protocol

from trendstorm.domain.reports.models import Report


class ReportRepository(Protocol):
    """Persistence contract for Reports."""

    async def insert(self, report: Report) -> None: ...

    async def get(self, tenant_id: str, report_id: str) -> Report | None: ...

    async def list_by_job(self, tenant_id: str, job_id: str) -> list[Report]:
        """All formats rendered for a job."""
        ...
