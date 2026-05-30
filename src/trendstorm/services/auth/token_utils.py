"""Shared token helpers for auth services.

All tokens (invite, password-reset, email-verification, refresh) follow the
same pattern: 32 random bytes → URL-safe base64 → SHA-256 hash stored in Mongo.
The plaintext is included in the email link; the hash is what we look up.
"""

from __future__ import annotations

import hashlib
import secrets

_TOKEN_BYTES = 32


def generate_token() -> str:
    """Return a new plaintext token (URL-safe base64, 32 bytes ≈ 43 chars)."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def hash_token(plaintext: str) -> str:
    """Return the SHA-256 hex digest of a plaintext token."""
    return hashlib.sha256(plaintext.encode()).hexdigest()
