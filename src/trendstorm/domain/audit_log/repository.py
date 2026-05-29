"""AuditLogRepository Protocol — domain-level contract.

Concrete implementation: MongoAuditLogRepository.
"""

from __future__ import annotations

from typing import Protocol

from trendstorm.domain.audit_log.models import AuditLogEntry


class AuditLogRepository(Protocol):
    """Append-only security event log. No update or delete operations."""

    async def append(self, entry: AuditLogEntry) -> None:
        """Persist a new audit log entry. Never raises on duplicate (ULID IDs)."""
        ...

    async def list_for_tenant(
        self,
        tenant_id: str,
        *,
        event_type: str | None = None,
        limit: int = 100,
    ) -> list[AuditLogEntry]:
        """Return recent audit entries for a tenant, newest first."""
        ...
