"""Unit tests for infrastructure/blob/uri.py — pure functions, no I/O."""
from __future__ import annotations

import pytest

from trendstorm.infrastructure.blob.uri import (
    parse_s3_uri,
    raw_key,
    report_key,
    text_key,
    to_s3_uri,
)

T = "tenant1"
J = "job01"
D = "doc01"


@pytest.mark.unit
class TestKeys:
    def test_raw_key_structure(self) -> None:
        assert raw_key(T, J, D) == f"{T}/{J}/{D}/raw.html"

    def test_text_key_structure(self) -> None:
        assert text_key(T, J, D) == f"{T}/{J}/{D}/text.txt"

    def test_report_key_default_format(self) -> None:
        assert report_key(T, J, D) == f"{T}/{J}/{D}/report.md"

    def test_report_key_custom_format(self) -> None:
        assert report_key(T, J, D, fmt="pdf") == f"{T}/{J}/{D}/report.pdf"

    def test_keys_are_distinct(self) -> None:
        assert raw_key(T, J, D) != text_key(T, J, D)
        assert text_key(T, J, D) != report_key(T, J, D)


@pytest.mark.unit
class TestToS3Uri:
    def test_combines_bucket_and_key(self) -> None:
        assert to_s3_uri("my-bucket", "a/b/c.txt") == "s3://my-bucket/a/b/c.txt"

    def test_roundtrip_with_raw_key(self) -> None:
        key = raw_key(T, J, D)
        uri = to_s3_uri("trendstorm-raw", key)
        assert uri.startswith("s3://trendstorm-raw/")
        assert uri.endswith("/raw.html")


@pytest.mark.unit
class TestParseS3Uri:
    def test_parses_valid_uri(self) -> None:
        bucket, key = parse_s3_uri("s3://my-bucket/path/to/file.txt")
        assert bucket == "my-bucket"
        assert key == "path/to/file.txt"

    def test_roundtrip(self) -> None:
        original = to_s3_uri("trendstorm-raw", raw_key(T, J, D))
        bucket, key = parse_s3_uri(original)
        assert bucket == "trendstorm-raw"
        assert key == raw_key(T, J, D)

    def test_rejects_non_s3_uri(self) -> None:
        with pytest.raises(ValueError, match="Not an S3 URI"):
            parse_s3_uri("https://example.com/file.txt")

    def test_rejects_uri_with_no_bucket(self) -> None:
        with pytest.raises(ValueError):
            parse_s3_uri("s3:///key-only")
