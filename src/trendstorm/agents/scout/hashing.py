"""Content hashing for deduplication.

SHA-256 of the *extracted text* (not the raw HTML). Two pages with identical
article text but different ads, nav, or tracking scripts hash the same —
avoiding redundant storage and re-embedding of identical content.

This is intentionally a module-level pure function so it can be tested
without any infrastructure and called from multiple pipeline stages.
"""
from __future__ import annotations

import hashlib


def content_hash(text: str) -> str:
    """Return the SHA-256 hex digest of the extracted text.

    Always UTF-8 encoded before hashing so the result is independent of the
    platform's default encoding. Returns a 64-character lowercase hex string.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
