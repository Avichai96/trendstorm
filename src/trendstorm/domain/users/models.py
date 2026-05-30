"""User domain model — top-level identity record.

Users are NOT tenant-scoped. A single user can belong to many organizations
via Membership records. The user's identity_provider_subject links to Auth0.

Lifecycle:
    active    → deleted_at=None, purge_at=None
    tombstoned → deleted_at=<timestamp>, purge_at=<timestamp+30d>
    cancelled  → deleted_at=None, purge_at=None (deletion cancelled in window)
    purged     → document removed from Mongo (hard delete by sweeper)
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from trendstorm.shared.ids import new_id


class User(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    email: str  # always lowercase; uniqueness via case-insensitive collation index
    email_verified: bool = False
    full_name: str | None = None
    avatar_url: str | None = None
    # Auth0 `sub` claim — e.g. "auth0|abc123" or "google-oauth2|456".
    # None until the IdP account is linked (rare; always set after signup).
    identity_provider_subject: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # Soft-delete tombstone. Set on deletion request.
    deleted_at: datetime | None = None
    # Hard-delete trigger. Set to deleted_at + 30 days by account_deletion_service.
    # The purge sweeper picks up documents where purge_at <= now().
    purge_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.deleted_at is None

    @property
    def is_pending_purge(self) -> bool:
        return self.deleted_at is not None and self.purge_at is not None
