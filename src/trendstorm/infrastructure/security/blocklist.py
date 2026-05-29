"""Global blocklist loader and per-host check helpers.

The global blocklist is loaded once at module import from
ops/security/global-blocklist.txt (located relative to the package root).
It is a frozenset of lowercase strings: hostnames, suffixes (starting with "."),
and IP literals. CIDR ranges are handled by the SSRF validator's _BLOCKED_NETWORKS.

Per-tenant blocklist entries are fetched from MongoDB via UrlBlocklistRepository
and cached for a short window (60 s) per tenant to avoid per-request DB lookups.

Usage (in Scout fetcher):
    from trendstorm.infrastructure.security.blocklist import (
        check_global_blocklist, check_tenant_blocklist
    )
    check_global_blocklist(hostname, url)         # raises SSRFBlockedError if hit
    await check_tenant_blocklist(hostname, url,   # same
        tenant_id=t, repo=blocklist_repo)
"""

from __future__ import annotations

import asyncio
import pathlib
import time
from urllib.parse import urlsplit

from trendstorm.shared.errors import SSRFBlockedError
from trendstorm.shared.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Global blocklist (loaded once at import)
# ---------------------------------------------------------------------------

_BLOCKLIST_PATH = (
    pathlib.Path(__file__).parent.parent.parent.parent.parent.parent
    / "ops"
    / "security"
    / "global-blocklist.txt"
)

_GLOBAL_DOMAINS: frozenset[str] = frozenset()
_GLOBAL_SUFFIXES: frozenset[str] = frozenset()


def _load_global_blocklist() -> tuple[frozenset[str], frozenset[str]]:
    """Parse global-blocklist.txt into domain and suffix sets."""
    domains: set[str] = set()
    suffixes: set[str] = set()

    if not _BLOCKLIST_PATH.exists():
        logger.warning("global_blocklist_not_found", path=str(_BLOCKLIST_PATH))
        return frozenset(), frozenset()

    for line in _BLOCKLIST_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        lower = line.lower()
        if lower.startswith("."):
            suffixes.add(lower)
        else:
            domains.add(lower)

    return frozenset(domains), frozenset(suffixes)


# Load at module import — this is a small file, reads in < 1 ms.
try:
    _GLOBAL_DOMAINS, _GLOBAL_SUFFIXES = _load_global_blocklist()
except Exception as exc:
    logger.error("global_blocklist_load_error", error=str(exc))


def check_global_blocklist(hostname: str, url: str) -> None:
    """Raise SSRFBlockedError if hostname matches the global blocklist."""
    lower = hostname.lower()
    if lower in _GLOBAL_DOMAINS:
        raise SSRFBlockedError(
            f"Host {hostname!r} is on the global blocklist",
            reason="ssrf_blocklist_global",
            url=url,
        )
    for suffix in _GLOBAL_SUFFIXES:
        if lower.endswith(suffix):
            raise SSRFBlockedError(
                f"Host {hostname!r} matches global blocklist suffix {suffix!r}",
                reason="ssrf_blocklist_global",
                url=url,
            )


# ---------------------------------------------------------------------------
# Per-tenant blocklist (simple in-process cache, 60 s TTL)
# ---------------------------------------------------------------------------

_TENANT_CACHE: dict[str, tuple[float, frozenset[str], frozenset[str]]] = {}
_CACHE_TTL = 60.0  # seconds
_CACHE_LOCK = asyncio.Lock()


async def check_tenant_blocklist(
    hostname: str,
    url: str,
    *,
    tenant_id: str,
    repo: object,
) -> None:
    """Raise SSRFBlockedError if hostname matches a per-tenant blocklist entry.

    Args:
        hostname: The hostname extracted from the URL being fetched.
        url: The full URL (used in the error context).
        tenant_id: The tenant scope for the blocklist lookup.
        repo: A UrlBlocklistRepository instance (duck-typed to avoid circular import).

    """
    domains, suffixes = await _get_tenant_lists(tenant_id, repo)
    lower = hostname.lower()
    if lower in domains:
        raise SSRFBlockedError(
            f"Host {hostname!r} is on the tenant blocklist",
            reason="ssrf_blocklist_tenant",
            url=url,
        )
    for suffix in suffixes:
        if lower.endswith(suffix):
            raise SSRFBlockedError(
                f"Host {hostname!r} matches tenant blocklist suffix {suffix!r}",
                reason="ssrf_blocklist_tenant",
                url=url,
            )


async def _get_tenant_lists(tenant_id: str, repo: object) -> tuple[frozenset[str], frozenset[str]]:
    now = time.monotonic()
    cached = _TENANT_CACHE.get(tenant_id)
    if cached is not None and now - cached[0] < _CACHE_TTL:
        return cached[1], cached[2]

    async with _CACHE_LOCK:
        # Double-check under lock
        cached = _TENANT_CACHE.get(tenant_id)
        if cached is not None and now - cached[0] < _CACHE_TTL:
            return cached[1], cached[2]

        try:
            entries = await repo.list_for_tenant(tenant_id)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.warning("tenant_blocklist_fetch_error", tenant_id=tenant_id, error=str(exc))
            # Fail open on DB error — SSRF rules still apply via global blocklist
            return frozenset(), frozenset()

        domains: set[str] = set()
        suffixes: set[str] = set()
        for entry in entries:
            p = entry.pattern.lower()
            if entry.pattern_type == "suffix":
                suffixes.add(p if p.startswith(".") else f".{p}")
            elif entry.pattern_type in {"domain", "cidr"}:
                domains.add(p)

        result = (frozenset(domains), frozenset(suffixes))
        _TENANT_CACHE[tenant_id] = (now, result[0], result[1])
        return result


def _extract_hostname(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()
