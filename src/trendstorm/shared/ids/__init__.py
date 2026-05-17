"""ID generation utilities.

Why ULID instead of UUID v4?
    - Lexicographically sortable (time-prefix) — natural sort in Mongo without
      a separate created_at index for ordering.
    - 26 chars, URL-safe, case-insensitive Crockford base32.
    - 128 bits like UUID, same collision properties.
    - Strictly more useful than UUID v4 for our use case.

Why not UUID v7?
    - UUID v7 is similar (time-ordered) but newer. python-ulid is more mature
      and we get the same benefits.

Why not int IDs?
    - Distributed generation: we have many writers. ULID is collision-free
      across machines without coordination. Int sequences require a single
      generator (Mongo allows it via counters, but adds a write per ID).
    - Privacy: int IDs leak business volume (`/jobs/12345` tells competitors
      we have at least 12k jobs).
"""
from __future__ import annotations

from ulid import ULID


def new_id() -> str:
    """Generate a new ULID as its canonical 26-char string."""
    return str(ULID())


def is_valid_id(value: str) -> bool:
    """Validate a string is a well-formed ULID."""
    try:
        ULID.from_str(value)
    except (ValueError, TypeError):
        return False
    return True
