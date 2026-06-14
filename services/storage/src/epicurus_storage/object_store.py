"""App-managed object storage — tenant-scoped put/get via MinIO (S3-compatible).

The read-only file-tree index (scanner.py + db.py) covers the operator's existing
HDD. This module covers *objects the platform itself creates*: generated files,
exports, attachments, etc.  Each tenant gets an isolated bucket named via the
epicurus-core ``scope_bucket`` convention (``{tenant}-storage``).

Two surfaces sit on the same bucket: text put/get (used by the ``storage_object_*``
agent tools) and **binary** put/get (used by the chat upload sink — ADR-0025 — which
streams arbitrary file bytes with their content type).
"""

from __future__ import annotations

from dataclasses import dataclass

import aioboto3
from botocore.exceptions import ClientError

from epicurus_core import get_logger
from epicurus_core.tenancy import scope_bucket

log = get_logger("storage.objects")

# S3 error codes MinIO returns for missing resources.
_BUCKET_MISSING = {"NoSuchBucket", "404"}
_KEY_MISSING = {"NoSuchKey", "404"}

_DEFAULT_CONTENT_TYPE = "application/octet-stream"


@dataclass(frozen=True)
class StoredObject:
    """A retrieved object's bytes plus the content type it was stored with."""

    data: bytes
    content_type: str


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

    # ── Binary surface (chat upload sink, ADR-0025) ──────────────────────────

    async def put_bytes(
        self, *, tenant: str, key: str, data: bytes, content_type: str = _DEFAULT_CONTENT_TYPE
    ) -> None:
        """Store raw *data* at *key* with *content_type* in the tenant's bucket."""
        await self._ensure_bucket(tenant)
        bucket = self._bucket(tenant)
        async with self._session.client("s3", endpoint_url=self._url) as s3:
            await s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=data,
                ContentType=content_type or _DEFAULT_CONTENT_TYPE,
            )
        log.debug("object stored", bucket=bucket, key=key, tenant=tenant, bytes=len(data))

    async def get_object(self, *, tenant: str, key: str) -> StoredObject | None:
        """Retrieve raw bytes + content type at *key*, or ``None`` if absent."""
        bucket = self._bucket(tenant)
        async with self._session.client("s3", endpoint_url=self._url) as s3:
            try:
                response = await s3.get_object(Bucket=bucket, Key=key)
                body: bytes = await response["Body"].read()
                content_type: str = response.get("ContentType") or _DEFAULT_CONTENT_TYPE
                return StoredObject(data=body, content_type=content_type)
            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                if code in _KEY_MISSING | _BUCKET_MISSING:
                    return None
                raise

    # ── Text surface (storage_object_* tools) ────────────────────────────────

    async def put(self, *, tenant: str, key: str, content: str) -> None:
        """Store *content* (UTF-8 text) at *key* in the tenant's object bucket."""
        await self.put_bytes(
            tenant=tenant, key=key, data=content.encode("utf-8"), content_type="text/plain"
        )

    async def get(self, *, tenant: str, key: str) -> str | None:
        """Retrieve text content at *key*, or ``None`` if the key does not exist."""
        stored = await self.get_object(tenant=tenant, key=key)
        return None if stored is None else stored.data.decode("utf-8")
