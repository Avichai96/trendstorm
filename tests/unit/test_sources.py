"""Unit tests for source URL canonicalization and the Source model.

These are pure-function tests — no DB, no I/O. They pin down the
canonicalization contract because every dedup decision flows from it.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from trendstorm.domain.sources.models import (
    Source,
    canonicalize_url,
    url_hash,
)
from trendstorm.shared.ids import new_id
from trendstorm.shared.types import SourceType


@pytest.mark.unit
class TestCanonicalize:
    """Two URLs that point to the same resource MUST canonicalize identically."""

    def test_lowercase_host(self) -> None:
        a = canonicalize_url("https://Example.COM/path")
        b = canonicalize_url("https://example.com/path")
        assert a == b

    def test_sort_query_params(self) -> None:
        a = canonicalize_url("https://e.com/?b=2&a=1")
        b = canonicalize_url("https://e.com/?a=1&b=2")
        assert a == b

    def test_drop_tracking_params(self) -> None:
        a = canonicalize_url("https://e.com/post?utm_source=x&id=5")
        b = canonicalize_url("https://e.com/post?id=5")
        assert a == b

    def test_drop_fragment(self) -> None:
        a = canonicalize_url("https://e.com/post#section")
        b = canonicalize_url("https://e.com/post")
        assert a == b

    def test_strip_trailing_slash(self) -> None:
        a = canonicalize_url("https://e.com/post/")
        b = canonicalize_url("https://e.com/post")
        assert a == b

    def test_default_https_port_dropped(self) -> None:
        a = canonicalize_url("https://e.com:443/x")
        b = canonicalize_url("https://e.com/x")
        assert a == b

    def test_non_default_port_kept(self) -> None:
        # Non-standard port DOES change identity.
        a = canonicalize_url("https://e.com:8443/x")
        b = canonicalize_url("https://e.com/x")
        assert a != b

    def test_different_paths_stay_different(self) -> None:
        """Sanity: not everything collapses."""
        a = canonicalize_url("https://e.com/a")
        b = canonicalize_url("https://e.com/b")
        assert a != b


@pytest.mark.unit
class TestUrlHash:
    def test_deterministic(self) -> None:
        h1 = url_hash("https://e.com/x")
        h2 = url_hash("https://e.com/x")
        assert h1 == h2

    def test_hex_length(self) -> None:
        # SHA-256 hex is 64 chars.
        assert len(url_hash("https://e.com/")) == 64


@pytest.mark.unit
class TestSourceModel:
    def _base_kwargs(self) -> dict[str, str]:
        return {
            "tenant_id": new_id(),
            "category_id": new_id(),
            "url": "https://example.com/feed",
        }

    def test_url_hash_auto_populated(self) -> None:
        s = Source(**self._base_kwargs())
        assert len(s.url_hash) == 64

    def test_same_canonical_url_same_hash(self) -> None:
        kwargs = self._base_kwargs()
        a = Source(**kwargs, label="A")
        b = Source(**{**kwargs, "url": "HTTPS://Example.com/feed?utm_source=fb"}, label="B")
        assert a.url_hash == b.url_hash

    def test_invalid_url_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Source(
                tenant_id=new_id(),
                category_id=new_id(),
                url="not-a-url",
            )

    def test_ftp_scheme_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Source(
                tenant_id=new_id(),
                category_id=new_id(),
                url="ftp://e.com/file",
            )

    def test_default_type_is_http(self) -> None:
        s = Source(**self._base_kwargs())
        assert s.type == SourceType.HTTP

    def test_default_enabled_true(self) -> None:
        s = Source(**self._base_kwargs())
        assert s.enabled is True
