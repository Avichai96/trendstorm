"""Auth domain models.

Three tightly coupled models:
- `Tenant` — a paying customer. One row per organisation. In Phase 12 the
  tenant-header placeholder is replaced by real tenant identity derived from
  an authenticated key or JWT.
- `ApiKey` — a bearer credential tied to a tenant. Never stored plaintext;
  only the SHA-256 hash is persisted. The prefix ("ts_live_XXXX") is stored
  for display ("you used key ts_live_Ab3c...") without exposing the secret.
- `AuthContext` — ephemeral per-request result of auth verification. Lives
  only in `request.state`; never persisted to Mongo.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from trendstorm.shared.ids import new_id

TenantPlan = Literal["free", "pro", "enterprise"]
AuthSource = Literal["api_key", "jwt", "legacy"]


class Tenant(BaseModel):
    """A tenant (customer organisation).

    Tenants own all data in multi-tenant collections. The `plan` field is
    used by the cost governor to apply quota tiers from `TenantQuotas`.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    name: str
    plan: TenantPlan = "free"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ApiKey(BaseModel):
    """A hashed API key credential.

    The plaintext key is shown to the user ONCE on creation and is never
    recoverable thereafter. We store only `key_hash` (SHA-256 hex) for
    constant-time comparison, and `key_prefix` (first 8 chars of the random
    portion) for display.

    Key format: `ts_{env}_{32_url_safe_random_chars}`
    Hash format: `sha256(plaintext_key)` as lowercase hex

    Roles: a list of role strings (e.g. ["reviewer"]). Empty list = standard
    access. Roles are per-key so a tenant can have separate app keys and
    reviewer keys with different permissions. Default empty — backward-compatible.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    tenant_id: str
    name: str  # human label (e.g. "CI pipeline", "mobile app")
    key_hash: str  # SHA-256 hex of the plaintext key
    key_prefix: str  # first 8 chars of the random portion (for display)
    roles: list[str] = Field(default_factory=list)  # e.g. ["reviewer"]

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_active(self) -> bool:
        return not self.is_revoked


class AuthContext(BaseModel):
    """Per-request authentication result. Stored in `request.state.auth_context`.

    Downstream handlers read `tenant_id` from this; they never touch raw
    headers or JWT claims directly.

    roles: propagated from ApiKey.roles (key auth) or the JWT "roles" claim
    (JWT auth). Empty list = standard access. `require_role()` dependency checks this.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    key_id: str | None = None  # set when authenticated via API key
    subject: str | None = None  # JWT `sub` claim when authenticated via JWT
    source: AuthSource = "legacy"  # how auth was established
    roles: list[str] = Field(default_factory=list)  # e.g. ["reviewer"]
