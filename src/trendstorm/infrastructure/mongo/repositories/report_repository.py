"""MongoDB implementation of ReportRepository."""
from __future__ import annotations

from typing import ClassVar

from trendstorm.domain.reports.models import Report
from trendstorm.infrastructure.mongo.repositories._base import TenantScopedRepository
from trendstorm.infrastructure.mongo.schema import Collection


class MongoReportRepository(TenantScopedRepository[Report]):
    """Concrete ReportRepository backed by MongoDB."""

    collection: ClassVar[Collection] = Collection.REPORTS
    model: ClassVar[type[Report]] = Report

    async def insert(self, report: Report) -> None:
        await self._insert(self._encode(report), what=f"Report {report.id}")

    async def get(self, tenant_id: str, report_id: str) -> Report | None:
        doc = await self._find_one(
            self._tenant_query(tenant_id, _id=report_id),
            what=f"Report {report_id}",
        )
        return self._decode(doc) if doc else None

    async def list_by_job(self, tenant_id: str, job_id: str) -> list[Report]:
        docs = await self._find_many(
            self._tenant_query(tenant_id, job_id=job_id),
            sort=[("_id", -1)],
            what="reports by job",
        )
        return [self._decode(d) for d in docs]
