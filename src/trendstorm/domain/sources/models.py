"""Source domain model.

A Source is a registered data location (web page, RSS feed, JSON API)
that belongs to a Category. The ingestion pipeline (Phase 6) fetches
content from these sources.

Design choices:

- `url_hash`: SHA-256 of a canonicalized URL form. We index this for
  uniqueness rather than `url` itself for three reasons:
    1. Fixed size: index entries are 32 bytes, regardless of URL length.
       A 2000-char URL would cost 2KB per index entry; hashing keeps the
       index small.
    2. Canonicalization-safe: `https://Example.COM/path?b=2&a=1` and
       `https://example.com/path?a=1&b=2` are the same resource. We
       normalize before hashing so both produce identical hashes.
    3. Case sensitivity: Mongo string indexes are case-sensitive by
       default; collations exist but add overhead. Hashing sidesteps that.

- `fetch_strategy`: which Scout sub-agent handles this source. Default
  is `http` (single page fetch); RSS feeds use `rss` (paginated); APIs
  use `api` (auth/params).

- `last_fetch_at`, `last_fetch_status`: cached on the source for the UI's
  "X sources fetched successfully" display. These are written by the
  Scout (Phase 6) and tolerated as eventually consistent — if a source
  was fetched but the cache update failed, the next run still works.

We do NOT store:
- The fetched content itself (lives in `raw_documents`).
- Credentials (those go in `secrets/`, encrypted, Phase 12).
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from trendstorm.shared.ids import new_id
from trendstorm.shared.types import SourceType

# ---------------------------------------------------------------------------
# URL canonicalization
# ---------------------------------------------------------------------------

# Strip these tracking params before hashing — they don't affect the
# resource identity but cause cache misses.
_TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "gclid",
        "fbclid",
        "mc_cid",
        "mc_eid",
        "_ga",
        "ref",
    }
)


def canonicalize_url(raw: str) -> str:
    """Return a canonical form for content-identity comparison.

    Steps:
        - Lowercase host (per RFC 3986, host is case-insensitive).
        - Sort query parameters alphabetically.
        - Remove tracking parameters.
        - Strip trailing slash from path (treat /foo and /foo/ as same).
        - Drop fragment (it's client-side, doesn't change the resource).

    Does NOT:
        - Resolve redirects (would require I/O).
        - Decode percent-encoding (`%20` and ` ` are technically different;
          we treat them as equivalent only when input is consistent).
    """
    parts = urlsplit(raw.strip())
    scheme = parts.scheme.lower() or "https"
    host = parts.hostname.lower() if parts.hostname else ""
    port = f":{parts.port}" if parts.port and not _is_default_port(scheme, parts.port) else ""
    netloc = f"{host}{port}"
    path = re.sub(r"/+$", "", parts.path) or "/"

    # Filter + sort query params for stability.
    params = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    ]
    params.sort()
    query = urlencode(params)

    return urlunsplit((scheme, netloc, path, query, ""))


def _is_default_port(scheme: str, port: int) -> bool:
    return (scheme == "https" and port == 443) or (scheme == "http" and port == 80)


def url_hash(canonical: str) -> str:
    """SHA-256 hex of the canonicalized URL. Used as the dedup key."""
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class Source(BaseModel):
    """A registered data source within a Category."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    tenant_id: str
    category_id: str

    url: str = Field(..., min_length=4, max_length=4096)
    url_hash: str = ""  # populated by validator; never set manually
    label: str | None = Field(default=None, max_length=200)
    type: SourceType = SourceType.HTTP

    # Cached fetch status — eventually consistent, written by Scout (Phase 6).
    last_fetch_at: datetime | None = None
    last_fetch_status: str | None = None
    last_fetch_error: str | None = None
    enabled: bool = True

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        v = v.strip()
        parts = urlsplit(v)
        if not parts.scheme or not parts.hostname:
            raise ValueError("URL must include scheme and host")
        if parts.scheme not in {"http", "https"}:
            raise ValueError(f"Unsupported URL scheme: {parts.scheme}")
        return v

    def model_post_init(self, __context: object) -> None:
        # Compute url_hash AFTER URL validation. We set via object.__setattr__
        # because the model is mutable-by-default but we want to be explicit
        # that this is a derived field.
        if not self.url_hash:
            object.__setattr__(self, "url_hash", url_hash(canonicalize_url(self.url)))
