"""MongoDB implementation of AuditLogRepository."""

from __future__ import annotations

from typing import ClassVar

from pymongo import DESCENDING

from trendstorm.domain.audit_log.models import AuditLogEntry
from trendstorm.infrastructure.mongo.repositories._base import (
    TenantScopedRepository,
)
from trendstorm.infrastructure.mongo.schema import Collection
from trendstorm.shared.logging import get_logger

logger = get_logger(__name__)


class MongoAuditLogRepository(TenantScopedRepository[AuditLogEntry]):
    """Append-only audit log backed by MongoDB.

    Entries are never updated; the TTL index handles expiry after 365 days.
    Insert failures are logged as warnings rather than raised — a transient
    Mongo error must never abort the business logic that triggered the event.
    """

    collection: ClassVar[Collection] = Collection.AUDIT_LOG
    model: ClassVar[type[AuditLogEntry]] = AuditLogEntry

    async def append(self, entry: AuditLogEntry) -> None:
        try:
            await self._insert(self._encode(entry), what=f"AuditLogEntry {entry.id}")
        except Exception:
            # Audit log write must never crash the business path.
            logger.warning(
                "audit_log_write_failed",
                entry_id=entry.id,
                event_type=entry.event_type,
                tenant_id=entry.tenant_id,
            )

    async def list_for_tenant(
        self,
        tenant_id: str,
        *,
        event_type: str | None = None,
        limit: int = 100,
    ) -> list[AuditLogEntry]:
        query = self._tenant_query(tenant_id)
        if event_type is not None:
            query["event_type"] = event_type
        docs = await self._find_many(
            query,
            sort=[("created_at", DESCENDING)],
            limit=limit,
            what="AuditLogEntry list",
        )
        return [self._decode(d) for d in docs]
