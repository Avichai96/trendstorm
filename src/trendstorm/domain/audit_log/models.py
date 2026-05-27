"""Audit log domain model.

AuditLogEntry is an append-only security event record. The collection has
a 365-day TTL index; entries are never updated after insertion.

Actor encoding:
    "system"           — automated pipeline action (SSRF block, PII redaction)
    "api_key:<id>"     — action triggered by an authenticated API request
    "user:<id>"        — reserved for future interactive user actions

Outcome values (machine-readable for dashboards):
    "blocked"    — action was denied (SSRF, blocklist, auth)
    "redacted"   — content was modified (PII)
    "allowed"    — action passed all checks (security-positive audit)
    "detected"   — anomaly detected but not blocked (warning-level)
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from trendstorm.shared.ids import new_id


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AuditLogEntry(BaseModel):
    """Immutable security audit event.

    Created by infrastructure/security helpers; persisted by
    infrastructure/mongo/repositories/audit_log_repository.py.
    Never mutated after creation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=new_id)
    tenant_id: str
    event_type: str        # e.g. "ssrf_blocked", "pii_detected", "url_blocked"
    actor: str             # "system" | "api_key:<id>" | "user:<id>"
    resource_type: str     # "source" | "chunk" | "job" | "url"
    resource_id: str       # ULID of the resource being acted on (or the URL)
    action: str            # e.g. "validate_url", "redact_pii", "check_blocklist"
    outcome: str           # "blocked" | "redacted" | "allowed" | "detected"
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utc_now)
    trace_id: str | None = None
    correlation_id: str | None = None
