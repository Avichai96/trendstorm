"""AnalysisRepository protocol."""

from __future__ import annotations

from typing import Protocol

from trendstorm.domain.analyses.models import Analysis


class AnalysisRepository(Protocol):
    """Persistence contract for Analyses."""

    async def insert(self, analysis: Analysis) -> None: ...

    async def get(self, tenant_id: str, analysis_id: str) -> Analysis | None: ...

    async def get_for_job(self, tenant_id: str, job_id: str) -> Analysis | None:
        """One analysis per successful job. Sometimes the easier query."""
        ...

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
        """Call after the validator step in the orchestrator."""
        ...
