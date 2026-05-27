"""SSRF URL validator.

Entry point: `validate_url(url, *, resolved_addrs=None) -> ValidatedURL`

Raises SSRFBlockedError (a FetchError subclass) when the URL targets a
private/internal address or violates redirect policy. Never touches the
network itself — DNS resolution is optional and injectable, keeping the
core logic unit-testable without I/O.

Inner helpers are importable for table-driven unit tests:
    _check_scheme(scheme, url) -> None
    _check_scheme_downgrade(from_scheme, to_scheme, url) -> None
    _check_hostname_suffix(hostname, url) -> None
    _check_address(ip_str, url) -> None
    resolve_hostname(hostname) -> list[str]   # sync DNS, blocking

Design decisions:
    - Max 3 redirects hard-coded here (security constant, not config).
      Config's max_redirects may be higher; the SSRF limit is stricter.
    - Scheme downgrade (https→http) is blocked on redirects: the extra hop
      strips TLS and may land on an internal service that speaks HTTP only.
    - `resolved_addrs` override lets unit tests inject specific IPs without
      any socket calls.
    - `validate_redirect(from_url, to_url)` exposes the per-hop check so
      the Scout fetcher can validate each redirect step individually.
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

from trendstorm.shared.errors import SSRFBlockedError

# ---------------------------------------------------------------------------
# Security constants
# ---------------------------------------------------------------------------

MAX_REDIRECTS = 3

_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

# Internal DNS suffixes — never reachable from public internet
_INTERNAL_SUFFIXES: frozenset[str] = frozenset({
    ".internal",
    ".local",
    ".cluster.local",
    ".svc",
    ".svc.cluster.local",
    "localhost",
})

# All private / non-routable IPv4 and IPv6 networks
_BLOCKED_NETWORKS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    # RFC 1918 private ranges
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    # Loopback
    ipaddress.ip_network("127.0.0.0/8"),
    # Link-local (includes AWS IMDS 169.254.169.254)
    ipaddress.ip_network("169.254.0.0/16"),
    # Carrier-grade NAT (RFC 6598)
    ipaddress.ip_network("100.64.0.0/10"),
    # Documentation ranges (should not appear in real URLs)
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    # Broadcast
    ipaddress.ip_network("255.255.255.255/32"),
    # IPv6 loopback
    ipaddress.ip_network("::1/128"),
    # IPv6 ULA (fc00::/7 covers fd00::/8)
    ipaddress.ip_network("fc00::/7"),
    # IPv6 link-local
    ipaddress.ip_network("fe80::/10"),
    # IPv6 site-local (deprecated but still risky)
    ipaddress.ip_network("fec0::/10"),
    # IPv4-mapped IPv6 addresses (::ffff:0:0/96)
    ipaddress.ip_network("::ffff:0:0/96"),
]


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ValidatedURL:
    """A URL that has passed SSRF validation."""

    url: str      # original URL string
    host: str     # extracted hostname
    scheme: str   # normalized scheme


# ---------------------------------------------------------------------------
# Pure inner helpers (exported for unit tests)
# ---------------------------------------------------------------------------

def _check_scheme(scheme: str, url: str) -> None:
    """Reject non-HTTP(S) schemes — blocks file://, ftp://, gopher://, etc."""
    if scheme not in _ALLOWED_SCHEMES:
        raise SSRFBlockedError(
            f"Scheme {scheme!r} is not allowed",
            reason="ssrf_scheme_not_allowed",
            url=url,
        )


def _check_scheme_downgrade(from_scheme: str, to_scheme: str, url: str) -> None:
    """Reject https → http redirect: strips TLS, may expose internal services."""
    if from_scheme == "https" and to_scheme == "http":
        raise SSRFBlockedError(
            f"Redirect from HTTPS to HTTP is blocked",
            reason="ssrf_scheme_downgrade",
            url=url,
        )


def _check_hostname_suffix(hostname: str, url: str) -> None:
    """Reject hostnames with internal DNS suffixes and bare 'localhost'."""
    lower = hostname.lower()
    if lower == "localhost" or lower in _INTERNAL_SUFFIXES:
        raise SSRFBlockedError(
            f"Hostname {hostname!r} is an internal DNS name",
            reason="ssrf_internal_hostname",
            url=url,
        )
    for suffix in _INTERNAL_SUFFIXES:
        if suffix != "localhost" and lower.endswith(suffix):
            raise SSRFBlockedError(
                f"Hostname {hostname!r} ends with internal suffix {suffix!r}",
                reason="ssrf_internal_hostname",
                url=url,
            )


def _check_address(ip_str: str, url: str) -> None:
    """Reject IPs that fall inside private/blocked networks.

    ip_str may be IPv4 or IPv6. Raises SSRFBlockedError with the
    appropriate reason label for each network class.
    """
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        # Not a valid IP — leave hostname checks to _check_hostname_suffix
        return

    for net in _BLOCKED_NETWORKS:
        if addr in net:
            reason = _reason_for_network(net)
            raise SSRFBlockedError(
                f"IP address {ip_str!r} is in blocked range {str(net)!r}",
                reason=reason,
                url=url,
            )


def _reason_for_network(net: ipaddress.IPv4Network | ipaddress.IPv6Network) -> str:
    """Map a blocked network to a Prometheus reason label."""
    s = str(net)
    if s in {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"}:
        return "ssrf_private_ip"
    if s in {"127.0.0.0/8"}:
        return "ssrf_loopback"
    if s in {"169.254.0.0/16"}:
        return "ssrf_link_local"
    if s in {"::1/128"}:
        return "ssrf_ipv6_loopback"
    if s in {"fc00::/7", "fec0::/10"}:
        return "ssrf_ipv6_ula"
    if s in {"fe80::/10"}:
        return "ssrf_ipv6_link_local"
    if s in {"::ffff:0:0/96"}:
        return "ssrf_ipv4_mapped"
    return "ssrf_private_ip"


# ---------------------------------------------------------------------------
# DNS resolution (blocking — call from executor in async context)
# ---------------------------------------------------------------------------

def resolve_hostname(hostname: str) -> list[str]:
    """Synchronous DNS resolution. Returns all resolved IP addresses.

    Callers in async contexts must wrap in `loop.run_in_executor(None, ...)`.
    Returns an empty list if hostname is already a bare IP (no lookup needed).
    """
    try:
        ipaddress.ip_address(hostname)
        # Already an IP literal — return it directly
        return [hostname]
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(hostname, None)
        return [info[4][0] for info in infos]
    except socket.gaierror:
        # DNS resolution failed — block the URL (fail-closed)
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_url(
    url: str,
    *,
    resolved_addrs: list[str] | None = None,
) -> ValidatedURL:
    """Validate a URL against SSRF rules.

    Args:
        url: The URL to validate.
        resolved_addrs: Override for DNS resolution (unit tests only).
            Pass an explicit list of IPs to avoid network I/O.
            Pass [] to simulate a DNS failure (blocks the URL).

    Returns:
        ValidatedURL if the URL is safe.

    Raises:
        SSRFBlockedError: If the URL violates any SSRF rule.

    Note: This function does NOT follow redirects. Use validate_redirect()
    for each redirect hop. DNS resolution is synchronous (blocking); call
    from asyncio.get_event_loop().run_in_executor(None, ...) in async code.
    """
    parsed = urlsplit(url)

    scheme = (parsed.scheme or "").lower()
    hostname = (parsed.hostname or "").lower()

    _check_scheme(scheme, url)

    if not hostname:
        raise SSRFBlockedError(
            "URL has no hostname",
            reason="ssrf_no_hostname",
            url=url,
        )

    _check_hostname_suffix(hostname, url)

    # DNS resolution — use injected addrs in tests, real lookup in production
    if resolved_addrs is None:
        addrs = resolve_hostname(hostname)
    else:
        addrs = resolved_addrs

    if not addrs:
        raise SSRFBlockedError(
            f"DNS resolution failed for {hostname!r}",
            reason="ssrf_dns_failure",
            url=url,
        )

    for addr in addrs:
        _check_address(addr, url)

    return ValidatedURL(url=url, host=hostname, scheme=scheme)


def validate_redirect(
    from_url: str,
    to_url: str,
    *,
    resolved_addrs: list[str] | None = None,
) -> ValidatedURL:
    """Validate a single redirect hop.

    Enforces all SSRF rules on `to_url`, plus scheme-downgrade check
    between `from_url` and `to_url`.

    Args:
        from_url: The URL that issued the redirect.
        to_url: The redirect target to validate.
        resolved_addrs: IP override for unit tests (applied to to_url).

    Returns:
        ValidatedURL for the redirect target.

    Raises:
        SSRFBlockedError: On any SSRF violation.
    """
    from_scheme = (urlsplit(from_url).scheme or "").lower()
    to_parsed = urlsplit(to_url)
    to_scheme = (to_parsed.scheme or "").lower()

    _check_scheme_downgrade(from_scheme, to_scheme, to_url)
    return validate_url(to_url, resolved_addrs=resolved_addrs)
