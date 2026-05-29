"""RawDocumentRepository protocol."""

from __future__ import annotations

from typing import Protocol

from trendstorm.domain.documents.models import RawDocument


class RawDocumentRepository(Protocol):
    """Persistence contract for RawDocument metadata."""

    async def insert(self, document: RawDocument) -> None: ...

    async def get(self, tenant_id: str, document_id: str) -> RawDocument | None: ...

    async def find_by_content_hash(
        self,
        tenant_id: str,
        content_hash: str,
    ) -> RawDocument | None:
        """Check if we have already ingested this exact content.

        Used by Scout at fetch time. If a hit comes back, Scout skips the
        ingestion work and reuses the existing document. CRITICAL for cost
        control with overlapping sources.
        """
        ...

    async def list_by_job(
        self,
        tenant_id: str,
        job_id: str,
    ) -> list[RawDocument]:
        """All docs for a job. Bounded by source count per job (~100s)."""
        ...
