"""MongoDB implementation of AnalysisRepository."""
from __future__ import annotations

from typing import ClassVar

from pymongo.errors import PyMongoError

from trendstorm.domain.analyses.models import Analysis
from trendstorm.infrastructure.mongo.repositories._base import (
    TenantScopedRepository,
    now_utc,
    raise_db_error,
)
from trendstorm.infrastructure.mongo.schema import Collection


class MongoAnalysisRepository(TenantScopedRepository[Analysis]):
    """Concrete AnalysisRepository backed by MongoDB."""

    collection: ClassVar[Collection] = Collection.ANALYSES
    model: ClassVar[type[Analysis]] = Analysis

    async def insert(self, analysis: Analysis) -> None:
        await self._insert(self._encode(analysis), what=f"Analysis {analysis.id}")

    async def get(self, tenant_id: str, analysis_id: str) -> Analysis | None:
        doc = await self._find_one(
            self._tenant_query(tenant_id, _id=analysis_id),
            what=f"Analysis {analysis_id}",
        )
        return self._decode(doc) if doc else None

    async def get_for_job(self, tenant_id: str, job_id: str) -> Analysis | None:
        doc = await self._find_one(
            self._tenant_query(tenant_id, job_id=job_id),
            what=f"Analysis for job {job_id}",
        )
        return self._decode(doc) if doc else None

    async def update_validation(
        self,
        tenant_id: str,
        analysis_id: str,
        *,
        validator_score: float,
        validator_passed: bool,
        validator_notes: str | None,
        refinement_loops: int,
    ) -> None:
        update = {
            "$set": {
                "validator_score": validator_score,
                "validator_passed": validator_passed,
                "validator_notes": validator_notes,
                "refinement_loops": refinement_loops,
                "updated_at": now_utc(),
            }
        }
        try:
            await self._coll.update_one(
                self._tenant_query(tenant_id, _id=analysis_id), update
            )
        except PyMongoError as e:
            raise_db_error(e, operation="update_validation", analysis_id=analysis_id)
