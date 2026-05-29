"""Category domain model.

A Category is a TENANT-OWNED named trend topic (e.g., "AI safety", "Crypto
regulation"). It groups Sources and is the scope for analysis runs.

Design choices:

- `name` is unique per tenant (enforced by a unique compound index in
  `indexes.py`). Two tenants can both have a "Crypto" category.
- `description` is free-form text used by the analyst as context.
- `keywords` is a list of additional search terms the analyst can use
  alongside the category name when retrieving from the vector store.
- We don't store the count of sources here. That's a denormalization
  trap — keeping the count in sync with writes to the `sources` collection
  requires either a transaction (expensive) or eventual consistency
  (confusing). UI lists fetch the count via a cheap aggregate.

We do NOT store:
- `last_analyzed_at` — derived from `jobs` table; lying about it is worse
  than computing it.
- `latest_report_id` — likewise derived.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from trendstorm.shared.ids import new_id


class Category(BaseModel):
    """A user-curated trend topic."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    tenant_id: str
    name: str = Field(..., min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    keywords: list[str] = Field(default_factory=list)

    # Soft-delete: archived categories are hidden from lists but kept for
    # historical jobs that reference them.
    archived: bool = False

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, v: str) -> str:
        # Trim accidental whitespace. Two categories named "Crypto" and
        # "Crypto " would otherwise both be created (different unique
        # constraint values) and the user would see duplicates.
        return v.strip()

    @field_validator("keywords")
    @classmethod
    def _normalize_keywords(cls, v: list[str]) -> list[str]:
        # Dedup case-insensitively but preserve original casing of first
        # occurrence. Strips empties.
        seen: set[str] = set()
        out: list[str] = []
        for kw in v:
            cleaned = kw.strip()
            if not cleaned:
                continue
            key = cleaned.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(cleaned)
        return out
