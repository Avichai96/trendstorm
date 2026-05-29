"""URL blocklist domain model.

UrlBlocklistEntry represents one rule in a per-tenant or global blocklist.
Pattern types:
    "domain"  — exact hostname match (e.g. "evil.example.com")
    "suffix"  — hostname suffix (e.g. ".evil.example.com" blocks all subdomains)
    "prefix"  — URL prefix match (e.g. "https://example.com/internal/")
    "cidr"    — IP CIDR range (e.g. "198.51.100.0/24")

The SSRF validator checks domain and CIDR patterns. Prefix patterns are
applied by the Scout fetcher after URL construction.

Global entries (tenant_id=None) are seeded from ops/security/global-blocklist.txt
and loaded as an in-memory frozenset at module load. They are NOT stored in
MongoDB — the file is the authoritative source.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from trendstorm.shared.ids import new_id


def _utc_now() -> datetime:
    return datetime.now(UTC)


class UrlBlocklistEntry(BaseModel):
    """One blocklist rule. Inserted by ops/admin; never updated in place."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=new_id)
    tenant_id: str  # scope to this tenant
    pattern: str  # the matching value
    pattern_type: Literal["domain", "suffix", "prefix", "cidr"] = "domain"
    reason: str = ""  # human note
    added_by: str = "system"  # actor
    created_at: datetime = Field(default_factory=_utc_now)
