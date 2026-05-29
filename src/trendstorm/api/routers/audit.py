"""Audit log router — security event read endpoint.

GET /v1/audit — paginated audit log for the current tenant (admin role only).

Covers SSRF blocks, PII detections, blocklist hits, and any other security
events recorded by the infrastructure/security helpers.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from trendstorm_shared import AuditLogEntryResponse, AuditLogListResponse

from trendstorm.api.deps import MongoDep
from trendstorm.infrastructure.mongo.repositories.audit_log_repository import (
    MongoAuditLogRepository,
)
from trendstorm.utils.headers_docs import require_role, require_tenant

router = APIRouter(
    prefix="/v1/audit",
    tags=["audit"],
    dependencies=[Depends(require_tenant), require_role("tenant_admin")],
)


@router.get(
    "",
    response_model=AuditLogListResponse,
    summary="List audit log entries for the tenant",
)
async def list_audit_entries(
    request: Request,
    mongo: MongoDep,
    event_type: Annotated[str | None, Query(description="Filter by event_type")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> AuditLogListResponse:
    """Return the most recent audit log entries for the tenant, newest first.

    Admin role required. Filters: event_type (ssrf_blocked, pii_detected, url_blocked).
    """
    repo = MongoAuditLogRepository(mongo)
    entries = await repo.list_for_tenant(
        request.state.tenant_id,
        event_type=event_type,
        limit=limit,
    )
    return AuditLogListResponse(
        items=[
            AuditLogEntryResponse(
                id=e.id,
                tenant_id=e.tenant_id,
                event_type=e.event_type,
                actor=e.actor,
                resource_type=e.resource_type,
                resource_id=e.resource_id,
                action=e.action,
                outcome=e.outcome,
                metadata=e.metadata,
                created_at=e.created_at,
                trace_id=e.trace_id,
                correlation_id=e.correlation_id,
            )
            for e in entries
        ],
        next_cursor=None,  # cursor pagination can be added when needed
    )
