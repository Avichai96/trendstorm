"""API key generation, hashing, and prefix extraction.

Key format:  ts_{env}_{32_url_safe_random_chars}
  env = "live" (production) or "test" (CI / sandbox)
  32 url-safe random chars ≈ 192 bits of entropy.

This format is detectable by secret scanners (GitHub Advanced Security,
GitGuardian) via the "ts_live_" / "ts_test_" prefix pattern — a leaked key
generates an alert before it can be exploited.

The plaintext key is shown to the user ONCE at creation. We store only the
SHA-256 hash for constant-time comparison and the first 8 chars of the random
portion for display (safe to expose — 8 chars of a 32-char secret is ~24%).

All functions here are pure (no I/O), fully testable, importable without any
infra clients.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Literal

KeyEnv = Literal["live", "test"]

_PREFIX = "ts"
_RANDOM_LENGTH = 32  # url-safe chars; ~192 bits of entropy
_DISPLAY_LENGTH = 8  # chars of random portion shown in the UI (safe)


def generate_api_key(env: KeyEnv = "live") -> str:
    """Return a new plaintext API key.

    Example: `ts_live_aB3dEfGhIjKlMnOpQrStUvWxYz012345`
    """
    random_part = secrets.token_urlsafe(_RANDOM_LENGTH)[:_RANDOM_LENGTH]
    return f"{_PREFIX}_{env}_{random_part}"


def hash_key(raw_key: str) -> str:
    """Return the SHA-256 hex digest of a plaintext key.

    This is the value stored in Mongo and compared on every request.
    Constant-time comparison is provided by `hmac.compare_digest` in the
    auth service — do NOT use `==` directly.
    """
    return hashlib.sha256(raw_key.encode()).hexdigest()


def key_prefix(raw_key: str) -> str:
    """Return the displayable prefix (first 8 chars of the random portion).

    Safe to display in the UI so users can identify which key is which without
    exposing the full secret.

    Example: `ts_live_aB3dEfGh...` → `aB3dEfGh`
    """
    # Format: ts_{env}_{random}
    # Split on _ with maxsplit=2 to handle "ts", env, and random parts.
    parts = raw_key.split("_", maxsplit=2)
    if len(parts) < 3:
        raise ValueError(f"Malformed API key: missing env prefix in {raw_key[:10]}…")
    return parts[2][:_DISPLAY_LENGTH]


def parse_env(raw_key: str) -> KeyEnv:
    """Extract the env tag from a raw key ('live' or 'test')."""
    parts = raw_key.split("_", maxsplit=2)
    if len(parts) < 2 or parts[1] not in ("live", "test"):
        raise ValueError(f"Malformed API key: unknown env in {raw_key[:10]}…")
    return parts[1]  # type: ignore[return-value]
