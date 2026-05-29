"""S3 URI helpers for blob storage paths.

All content paths follow the convention:
    s3://{bucket}/{tenant_id}/{job_id}/{doc_id}/{artifact}

Keeping path construction in one place means parsers, workers, and the API
all produce identical URIs — there is no ambiguity about trailing slashes,
encoding, or separator characters.
"""

from __future__ import annotations


def raw_key(tenant_id: str, job_id: str, doc_id: str) -> str:
    """Object key for raw fetched bytes (HTML, XML, JSON, plain text)."""
    return f"{tenant_id}/{job_id}/{doc_id}/raw.html"


def text_key(tenant_id: str, job_id: str, doc_id: str) -> str:
    """Object key for the parser-extracted plain text artifact."""
    return f"{tenant_id}/{job_id}/{doc_id}/text.txt"


def report_key(tenant_id: str, job_id: str, doc_id: str, *, fmt: str = "md") -> str:
    """Object key for a rendered report artifact."""
    return f"{tenant_id}/{job_id}/{doc_id}/report.{fmt}"


def to_s3_uri(bucket: str, key: str) -> str:
    """Combine bucket + key into a canonical s3:// URI."""
    return f"s3://{bucket}/{key}"


def parse_s3_uri(uri: str) -> tuple[str, str]:
    """Split a canonical s3:// URI into (bucket, key).

    Raises ValueError for non-S3 URIs.
    """
    if not uri.startswith("s3://"):
        raise ValueError(f"Not an S3 URI: {uri!r}")
    rest = uri[len("s3://") :]
    bucket, _, key = rest.partition("/")
    if not bucket:
        raise ValueError(f"S3 URI has no bucket: {uri!r}")
    return bucket, key
