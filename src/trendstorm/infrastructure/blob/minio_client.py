"""MinIO (S3-compatible) async client wrapper.

Owns the aioboto3 S3 client lifecycle. One instance per process.

Design mirrors the Redis and Mongo client wrappers:
    - connect() creates the client and verifies by listing the target bucket.
    - close() exits the aioboto3 async context manager.
    - health_check() is non-throwing, for readiness probes.
    - upload() is the single write primitive; callers construct keys via uri.py.

Why aioboto3 over minio-py?
    We standardize on the S3 API throughout the platform (MinIO in dev,
    real S3 in prod). aioboto3 gives us the same interface for both without
    any runtime flag. minio-py is MinIO-specific and lacks async support.

Two buckets are provisioned at startup if absent:
    - bucket_raw    raw fetched bytes + parsed text (per RawDocument)
    - bucket_reports rendered final reports (per Report)
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import aioboto3
from botocore.exceptions import BotoCoreError, ClientError

from trendstorm.shared.errors import BlobError
from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    from trendstorm.shared.config import BlobSettings

logger = get_logger(__name__)


class MinioClient:
    """Async S3-compatible client lifecycle manager."""

    def __init__(self, settings: BlobSettings) -> None:
        self._settings = settings
        self._session: aioboto3.Session | None = None
        self._ctx: Any = None       # aioboto3 async context manager
        self._client: Any = None    # underlying botocore S3 client

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    async def connect(self) -> None:
        """Open the S3 client and ensure required buckets exist. Idempotent."""
        if self._client is not None:
            return

        logger.info("blob_connecting", endpoint=self._settings.endpoint)
        self._session = aioboto3.Session()
        self._ctx = self._session.client(
            "s3",
            endpoint_url=self._settings.endpoint,
            aws_access_key_id=self._settings.access_key.get_secret_value(),
            aws_secret_access_key=self._settings.secret_key.get_secret_value(),
            region_name=self._settings.region,
        )
        try:
            self._client = await self._ctx.__aenter__()
            await self._ensure_buckets()
        except (ClientError, BotoCoreError) as exc:
            await self._safe_close()
            raise BlobError(
                "Blob storage connection failed during startup",
                context={"endpoint": self._settings.endpoint, "error": str(exc)},
            ) from exc
        logger.info("blob_connected", endpoint=self._settings.endpoint)

    async def close(self) -> None:
        """Close the S3 client. Idempotent."""
        await self._safe_close()

    async def _safe_close(self) -> None:
        if self._client is None:
            return
        logger.info("blob_closing")
        try:
            await self._ctx.__aexit__(None, None, None)
        except Exception as exc:
            logger.warning("blob_close_error", error=str(exc))
        finally:
            self._client = None
            self._ctx = None
            self._session = None

    async def _ensure_buckets(self) -> None:
        """Create any missing buckets. Called once at connect time."""
        for bucket in (self._settings.bucket_raw, self._settings.bucket_reports):
            try:
                await self._client.head_bucket(Bucket=bucket)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code not in ("404", "NoSuchBucket"):
                    raise
                try:
                    await self._client.create_bucket(Bucket=bucket)
                    logger.info("blob_bucket_created", bucket=bucket)
                except ClientError as create_exc:
                    # Race: another instance created it between head and create.
                    if create_exc.response.get("Error", {}).get("Code") != "BucketAlreadyOwnedByYou":
                        raise

    # ------------------------------------------------------------------ #
    # Operations                                                           #
    # ------------------------------------------------------------------ #

    async def upload(
        self,
        bucket: str,
        key: str,
        body: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> str:
        """Upload bytes to S3-compatible storage. Returns the s3:// URI."""
        if self._client is None:
            raise BlobError("Blob client not initialized; call connect() first")
        try:
            await self._client.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
            )
        except (ClientError, BotoCoreError) as exc:
            raise BlobError(
                f"Upload failed: s3://{bucket}/{key}",
                context={"bucket": bucket, "key": key, "error": str(exc)},
            ) from exc
        return f"s3://{bucket}/{key}"

    async def download(self, bucket: str, key: str) -> bytes:
        """Download an object and return its raw bytes."""
        if self._client is None:
            raise BlobError("Blob client not initialized; call connect() first")
        try:
            response = await self._client.get_object(Bucket=bucket, Key=key)
            return bytes(await response["Body"].read())
        except (ClientError, BotoCoreError) as exc:
            raise BlobError(
                f"Download failed: s3://{bucket}/{key}",
                context={"bucket": bucket, "key": key, "error": str(exc)},
            ) from exc

    # ------------------------------------------------------------------ #
    # Health                                                               #
    # ------------------------------------------------------------------ #

    async def health_check(self) -> bool:
        """Fast non-throwing health check for readiness probes."""
        if self._client is None:
            return False
        try:
            await self._client.head_bucket(Bucket=self._settings.bucket_raw)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # Accessors                                                            #
    # ------------------------------------------------------------------ #

    @property
    def settings(self) -> BlobSettings:
        return self._settings
