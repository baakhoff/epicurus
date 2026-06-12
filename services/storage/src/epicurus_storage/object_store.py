"""App-managed object storage — tenant-scoped put/get via MinIO (S3-compatible).

The read-only file-tree index (scanner.py + db.py) covers the operator's existing
HDD. This module covers *objects the platform itself creates*: generated files,
exports, attachments, etc.  Each tenant gets an isolated bucket named via the
epicurus-core ``scope_bucket`` convention (``{tenant}-storage``).
"""

from __future__ import annotations

import aioboto3
from botocore.exceptions import ClientError

from epicurus_core import get_logger
from epicurus_core.tenancy import scope_bucket

log = get_logger("storage.objects")

# S3 error codes MinIO returns for missing resources.
_BUCKET_MISSING = {"NoSuchBucket", "404"}
_KEY_MISSING = {"NoSuchKey", "404"}


class ObjectStore:
    """Async client for tenant-scoped object storage on a MinIO endpoint."""

    def __init__(self, *, url: str, access_key: str, secret_key: str) -> None:
        self._url = url
        self._session = aioboto3.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="us-east-1",
        )

    def _bucket(self, tenant: str) -> str:
        return scope_bucket("storage", tenant)

    async def _ensure_bucket(self, tenant: str) -> None:
        """Create the tenant bucket if it does not exist."""
        bucket = self._bucket(tenant)
        async with self._session.client("s3", endpoint_url=self._url) as s3:
            try:
                await s3.head_bucket(Bucket=bucket)
            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                if code in _BUCKET_MISSING:
                    await s3.create_bucket(Bucket=bucket)
                    log.info("created object bucket", bucket=bucket, tenant=tenant)
                else:
                    raise

    async def put(self, *, tenant: str, key: str, content: str) -> None:
        """Store *content* (UTF-8 text) at *key* in the tenant's object bucket."""
        await self._ensure_bucket(tenant)
        bucket = self._bucket(tenant)
        async with self._session.client("s3", endpoint_url=self._url) as s3:
            await s3.put_object(
                Bucket=bucket, Key=key, Body=content.encode("utf-8"), ContentType="text/plain"
            )
        log.debug("object stored", bucket=bucket, key=key, tenant=tenant)

    async def get(self, *, tenant: str, key: str) -> str | None:
        """Retrieve text content at *key*, or ``None`` if the key does not exist."""
        bucket = self._bucket(tenant)
        async with self._session.client("s3", endpoint_url=self._url) as s3:
            try:
                response = await s3.get_object(Bucket=bucket, Key=key)
                body: bytes = await response["Body"].read()
                return body.decode("utf-8")
            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                if code in _KEY_MISSING | _BUCKET_MISSING:
                    return None
                raise
