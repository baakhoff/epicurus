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

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import aioboto3
from botocore.exceptions import ClientError

from epicurus_core import get_logger
from epicurus_core.tenancy import scope_bucket

log = get_logger("storage.objects")

# S3 error codes MinIO returns for missing resources.
_BUCKET_MISSING = {"NoSuchBucket", "404"}
_KEY_MISSING = {"NoSuchKey", "404"}

_DEFAULT_CONTENT_TYPE = "application/octet-stream"

# How much of an object crosses the wire per chunk on a streamed read, and how large a part
# is on a streamed write. S3 fixes the multipart part floor at 5 MiB for every part but the
# last, so 8 MiB is comfortably above it while keeping the in-flight buffer small enough that
# a multi-gigabyte object never becomes a multi-gigabyte process.
_STREAM_CHUNK_BYTES = 1024 * 1024
_PART_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class StoredObject:
    """A retrieved object's bytes plus the content type it was stored with."""

    data: bytes
    content_type: str


@dataclass(frozen=True)
class ObjectStat:
    """An object's metadata, read without fetching a byte of it."""

    size: int
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

    # ── Streaming surface (tenant portability, #876) ─────────────────────────

    async def stat_object(self, *, tenant: str, key: str) -> ObjectStat | None:
        """Size + content type at *key*, or ``None`` — a ``HEAD``, so no bytes move.

        The export's listing needs both for every object it names; fetching them by reading
        each object would mean reading the whole bucket just to describe it.
        """
        bucket = self._bucket(tenant)
        async with self._session.client("s3", endpoint_url=self._url) as s3:
            try:
                response = await s3.head_object(Bucket=bucket, Key=key)
            except ClientError as exc:
                if exc.response["Error"]["Code"] in _KEY_MISSING | _BUCKET_MISSING:
                    return None
                raise
            return ObjectStat(
                size=int(response.get("ContentLength") or 0),
                content_type=str(response.get("ContentType") or _DEFAULT_CONTENT_TYPE),
            )

    async def open_object(
        self, *, tenant: str, key: str, chunk_size: int = _STREAM_CHUNK_BYTES
    ) -> AsyncIterator[bytes]:
        """Stream an object's bytes, chunk by chunk. Raises ``FileNotFoundError`` if absent.

        The counterpart to :meth:`get_object` for anything that may be large: an export must
        never decide that a 4 GB upload is a 4 GB allocation.
        """
        bucket = self._bucket(tenant)
        async with self._session.client("s3", endpoint_url=self._url) as s3:
            try:
                response = await s3.get_object(Bucket=bucket, Key=key)
            except ClientError as exc:
                if exc.response["Error"]["Code"] in _KEY_MISSING | _BUCKET_MISSING:
                    raise FileNotFoundError(key) from exc
                raise
            async for chunk in response["Body"].iter_chunks(chunk_size):
                yield chunk

    async def put_stream(
        self,
        *,
        tenant: str,
        key: str,
        chunks: AsyncIterator[bytes],
        content_type: str = _DEFAULT_CONTENT_TYPE,
    ) -> int:
        """Write *chunks* to *key* without ever holding the whole object; returns its size.

        Small objects go in one ``PutObject``; anything past a single part is switched to a
        multipart upload mid-stream, so the size never has to be known in advance. Multipart
        is also what makes a failed or refused transfer clean: the parts are aborted and the
        key keeps whatever it had, which is what lets the caller verify a digest *after* the
        last chunk and still not publish bad bytes.
        """
        await self._ensure_bucket(tenant)
        bucket = self._bucket(tenant)
        media = content_type or _DEFAULT_CONTENT_TYPE
        async with self._session.client("s3", endpoint_url=self._url) as s3:
            buffer = bytearray()
            parts: list[dict[str, Any]] = []
            upload_id: str | None = None
            total = 0
            try:
                async for chunk in chunks:
                    buffer += chunk
                    total += len(chunk)
                    while len(buffer) > _PART_BYTES:
                        if upload_id is None:
                            started = await s3.create_multipart_upload(
                                Bucket=bucket, Key=key, ContentType=media
                            )
                            upload_id = str(started["UploadId"])
                        payload = bytes(buffer[:_PART_BYTES])
                        del buffer[:_PART_BYTES]
                        parts.append(
                            await self._upload_part(s3, bucket, key, upload_id, parts, payload)
                        )
                if upload_id is None:
                    await s3.put_object(
                        Bucket=bucket, Key=key, Body=bytes(buffer), ContentType=media
                    )
                else:
                    parts.append(
                        await self._upload_part(s3, bucket, key, upload_id, parts, bytes(buffer))
                    )
                    await s3.complete_multipart_upload(
                        Bucket=bucket,
                        Key=key,
                        UploadId=upload_id,
                        MultipartUpload={"Parts": parts},
                    )
            except BaseException:
                if upload_id is not None:
                    await s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
                raise
        log.debug("object streamed", bucket=bucket, key=key, tenant=tenant, bytes=total)
        return total

    @staticmethod
    async def _upload_part(
        s3: Any, bucket: str, key: str, upload_id: str, parts: list[dict[str, Any]], data: bytes
    ) -> dict[str, Any]:
        """Upload one multipart part and return the ``{PartNumber, ETag}`` the complete needs."""
        number = len(parts) + 1
        response = await s3.upload_part(
            Bucket=bucket, Key=key, UploadId=upload_id, PartNumber=number, Body=data
        )
        return {"PartNumber": number, "ETag": response["ETag"]}

    async def copy(self, *, tenant: str, src_key: str, dst_key: str) -> None:
        """Server-side copy of an object from *src_key* to *dst_key* (S3 has no rename).

        The byte half of a move: the index re-key is the source of truth, so callers copy
        to the new key first, re-path the index, then drop the original.
        """
        bucket = self._bucket(tenant)
        async with self._session.client("s3", endpoint_url=self._url) as s3:
            await s3.copy_object(
                Bucket=bucket, Key=dst_key, CopySource={"Bucket": bucket, "Key": src_key}
            )

    async def delete(self, *, tenant: str, key: str) -> None:
        """Delete the object at *key* (a no-op if it is already gone)."""
        bucket = self._bucket(tenant)
        async with self._session.client("s3", endpoint_url=self._url) as s3:
            try:
                await s3.delete_object(Bucket=bucket, Key=key)
            except ClientError as exc:
                if exc.response["Error"]["Code"] not in _KEY_MISSING | _BUCKET_MISSING:
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
