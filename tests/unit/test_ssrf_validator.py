"""Unit tests for the SSRF URL validator.

All tests are pure (no I/O, no DNS). Injected resolved_addrs override DNS.
Table-driven where possible to exhaustively cover each rejection class.
"""
from __future__ import annotations

import pytest

from trendstorm.infrastructure.security.ssrf import (
    _check_address,
    _check_hostname_suffix,
    _check_scheme,
    _check_scheme_downgrade,
    validate_redirect,
    validate_url,
)
from trendstorm.shared.errors import SSRFBlockedError


# ---------------------------------------------------------------------------
# _check_scheme
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCheckScheme:
    @pytest.mark.parametrize("scheme", ["http", "https"])
    def test_allowed_schemes_pass(self, scheme: str) -> None:
        _check_scheme(scheme, f"{scheme}://example.com")  # no raise

    @pytest.mark.parametrize("scheme,url", [
        ("file", "file:///etc/passwd"),
        ("ftp", "ftp://files.example.com"),
        ("gopher", "gopher://gopher.example.com"),
        ("dict", "dict://dict.example.com"),
        ("sftp", "sftp://storage.example.com"),
        ("ldap", "ldap://corp.example.com"),
    ])
    def test_disallowed_schemes_raise(self, scheme: str, url: str) -> None:
        with pytest.raises(SSRFBlockedError) as exc_info:
            _check_scheme(scheme, url)
        assert exc_info.value.reason == "ssrf_scheme_not_allowed"


# ---------------------------------------------------------------------------
# _check_scheme_downgrade
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCheckSchemeDowngrade:
    def test_https_to_http_blocked(self) -> None:
        with pytest.raises(SSRFBlockedError) as exc_info:
            _check_scheme_downgrade("https", "http", "http://example.com/path")
        assert exc_info.value.reason == "ssrf_scheme_downgrade"

    @pytest.mark.parametrize("from_s,to_s", [
        ("http", "https"),   # upgrade — allowed
        ("http", "http"),    # same — allowed
        ("https", "https"),  # same — allowed
    ])
    def test_non_downgrade_passes(self, from_s: str, to_s: str) -> None:
        _check_scheme_downgrade(from_s, to_s, "https://example.com")  # no raise


# ---------------------------------------------------------------------------
# _check_hostname_suffix
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCheckHostnameSuffix:
    @pytest.mark.parametrize("hostname", [
        "localhost",
        "api.internal",
        "mongo.cluster.local",
        "redis.svc",
        "foo.svc.cluster.local",
        "my-service.local",
    ])
    def test_internal_hostnames_blocked(self, hostname: str) -> None:
        with pytest.raises(SSRFBlockedError) as exc_info:
            _check_hostname_suffix(hostname, f"http://{hostname}/")
        assert exc_info.value.reason == "ssrf_internal_hostname"

    @pytest.mark.parametrize("hostname", [
        "example.com",
        "api.example.org",
        "news.bbc.co.uk",
        "github.com",
    ])
    def test_public_hostnames_pass(self, hostname: str) -> None:
        _check_hostname_suffix(hostname, f"https://{hostname}/")  # no raise


# ---------------------------------------------------------------------------
# _check_address — IPv4
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCheckAddressIPv4:
    @pytest.mark.parametrize("ip,expected_reason", [
        # RFC 1918 private
        ("10.0.0.1", "ssrf_private_ip"),
        ("10.255.255.255", "ssrf_private_ip"),
        ("172.16.0.1", "ssrf_private_ip"),
        ("172.31.255.254", "ssrf_private_ip"),
        ("192.168.0.1", "ssrf_private_ip"),
        ("192.168.255.254", "ssrf_private_ip"),
        # Loopback
        ("127.0.0.1", "ssrf_loopback"),
        ("127.255.255.255", "ssrf_loopback"),
        # AWS / Azure / GCP metadata
        ("169.254.169.254", "ssrf_link_local"),
        ("169.254.0.1", "ssrf_link_local"),
        # CG-NAT (RFC 6598)
        ("100.64.0.1", "ssrf_private_ip"),
        ("100.127.255.255", "ssrf_private_ip"),
    ])
    def test_private_ipv4_blocked(self, ip: str, expected_reason: str) -> None:
        with pytest.raises(SSRFBlockedError) as exc_info:
            _check_address(ip, f"http://{ip}/")
        assert exc_info.value.reason == expected_reason

    @pytest.mark.parametrize("ip", [
        "8.8.8.8",
        "1.1.1.1",
        "151.101.64.81",
        "93.184.216.34",
    ])
    def test_public_ipv4_passes(self, ip: str) -> None:
        _check_address(ip, f"http://{ip}/")  # no raise


# ---------------------------------------------------------------------------
# _check_address — IPv6
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCheckAddressIPv6:
    @pytest.mark.parametrize("ip,expected_reason", [
        ("::1", "ssrf_ipv6_loopback"),
        ("fc00::1", "ssrf_ipv6_ula"),
        ("fd12:3456:789a::1", "ssrf_ipv6_ula"),
        ("fe80::1", "ssrf_ipv6_link_local"),
        ("fe80::dead:beef", "ssrf_ipv6_link_local"),
        ("::ffff:10.0.0.1", "ssrf_ipv4_mapped"),    # IPv4-mapped private
    ])
    def test_private_ipv6_blocked(self, ip: str, expected_reason: str) -> None:
        with pytest.raises(SSRFBlockedError) as exc_info:
            _check_address(ip, f"http://[{ip}]/")
        assert exc_info.value.reason == expected_reason

    @pytest.mark.parametrize("ip", [
        "2001:4860:4860::8888",   # Google Public DNS
        "2606:4700:4700::1111",   # Cloudflare
    ])
    def test_public_ipv6_passes(self, ip: str) -> None:
        _check_address(ip, f"http://[{ip}]/")  # no raise


# ---------------------------------------------------------------------------
# validate_url — integration of all checks (no DNS — injected addrs)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestValidateUrl:
    def test_valid_public_url_passes(self) -> None:
        result = validate_url(
            "https://example.com/path",
            resolved_addrs=["93.184.216.34"],
        )
        assert result.host == "example.com"
        assert result.scheme == "https"

    def test_private_ip_resolved_from_public_hostname_blocked(self) -> None:
        """DNS rebinding: public hostname resolves to private IP."""
        with pytest.raises(SSRFBlockedError) as exc_info:
            validate_url(
                "https://evil.example.com/",
                resolved_addrs=["10.0.0.1"],  # resolves to private
            )
        assert exc_info.value.reason == "ssrf_private_ip"

    def test_dns_failure_blocked(self) -> None:
        """Empty resolved_addrs = DNS failure = blocked."""
        with pytest.raises(SSRFBlockedError) as exc_info:
            validate_url("https://nxdomain.example/", resolved_addrs=[])
        assert exc_info.value.reason == "ssrf_dns_failure"

    def test_internal_hostname_blocked_before_dns(self) -> None:
        """Internal suffix check fires even with valid resolved IPs."""
        with pytest.raises(SSRFBlockedError) as exc_info:
            validate_url(
                "http://api.internal/",
                resolved_addrs=["93.184.216.34"],  # irrelevant
            )
        assert exc_info.value.reason == "ssrf_internal_hostname"

    def test_file_scheme_blocked(self) -> None:
        with pytest.raises(SSRFBlockedError) as exc_info:
            validate_url("file:///etc/passwd", resolved_addrs=[])
        assert exc_info.value.reason == "ssrf_scheme_not_allowed"

    def test_no_hostname_blocked(self) -> None:
        with pytest.raises(SSRFBlockedError) as exc_info:
            validate_url("https:///path/only", resolved_addrs=[])
        # Either scheme check or no-hostname check fires
        assert exc_info.value.reason in {
            "ssrf_no_hostname",
            "ssrf_scheme_not_allowed",
            "ssrf_internal_hostname",
        }

    def test_aws_metadata_ip_blocked(self) -> None:
        with pytest.raises(SSRFBlockedError) as exc_info:
            validate_url(
                "http://169.254.169.254/latest/meta-data/",
                resolved_addrs=["169.254.169.254"],
            )
        assert exc_info.value.reason == "ssrf_link_local"

    def test_loopback_direct_ip_blocked(self) -> None:
        with pytest.raises(SSRFBlockedError) as exc_info:
            validate_url("http://127.0.0.1/", resolved_addrs=["127.0.0.1"])
        assert exc_info.value.reason == "ssrf_loopback"


# ---------------------------------------------------------------------------
# validate_redirect
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestValidateRedirect:
    def test_https_to_https_allowed(self) -> None:
        result = validate_redirect(
            "https://a.example.com/",
            "https://b.example.com/",
            resolved_addrs=["93.184.216.34"],
        )
        assert result.scheme == "https"

    def test_http_to_https_allowed(self) -> None:
        validate_redirect(
            "http://a.example.com/",
            "https://b.example.com/",
            resolved_addrs=["93.184.216.34"],
        )

    def test_https_to_http_downgrade_blocked(self) -> None:
        with pytest.raises(SSRFBlockedError) as exc_info:
            validate_redirect(
                "https://a.example.com/",
                "http://b.example.com/",
                resolved_addrs=["93.184.216.34"],
            )
        assert exc_info.value.reason == "ssrf_scheme_downgrade"

    def test_redirect_to_private_ip_blocked(self) -> None:
        # http -> http (no scheme downgrade) to an address that resolves to RFC 1918
        with pytest.raises(SSRFBlockedError) as exc_info:
            validate_redirect(
                "http://public.example.com/",
                "http://internal.example.com/",
                resolved_addrs=["192.168.1.50"],
            )
        assert exc_info.value.reason == "ssrf_private_ip"
