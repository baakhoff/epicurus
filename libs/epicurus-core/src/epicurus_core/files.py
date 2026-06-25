"""The core-owned, tenant-scoped file space behind a swappable backend (ADR-0052).

Today each file-owning module mounts and writes the shared ``/data`` volume directly, so
the local filesystem is hardcoded — a violation of dual-track constraint #3 (storage must
sit behind a swappable local-FS ↔ S3 backend) for everything except the storage module's
object store. This module is the keystone of moving that ownership into the core: a single
``FileStore`` interface, tenant-scoped on every call (constraint #1), with a local-filesystem
backend (self-host) and an S3/MinIO backend (SaaS) behind the same contract.

The core exposes this over the platform API (``/platform/v1/files/*``); modules consume it
via :class:`~epicurus_core.platform_client.PlatformClient` instead of doing their own I/O.
This is Phase 1 (the abstraction + contract); migrating the modules off their direct mounts
is staged in follow-up work (see ADR-0052).
"""

from __future__ import annotations

import asyncio
import shutil
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel

from epicurus_core.tenancy import scope_bucket, validate_tenant_id

__all__ = [
    "FileEntry",
    "FileKind",
    "FileStore",
    "FileStoreBackend",
    "LocalFileStore",
    "S3FileStore",
    "build_file_store",
    "normalize_rel",
]

FileKind = Literal["file", "dir"]
FileStoreBackend = Literal["local", "s3"]

# Read cap for the text convenience helper — keeps an accidental multi-MB read from being
# pulled fully into memory. The raw byte API has no cap; callers stream large files instead.
_TEXT_READ_MAX = 256 * 1024


class FileEntry(BaseModel):
    """One node in the tenant file space — a file or a directory.

    ``path`` is the tenant-relative POSIX path (no leading slash, no tenant segment); ``size``
    and ``mtime`` are 0 for directories and for backends that do not report them.
    """

    path: str
    name: str
    kind: FileKind
    size: int = 0
    mtime: float = 0.0


def normalize_rel(path: str) -> str:
    """Reduce *path* to a safe tenant-relative POSIX path, or raise on traversal.

    Collapses back-slashes, drops empty / ``.`` segments, and **rejects** any ``..`` segment
    so a key can never escape its tenant root. Returns ``""`` for the tenant root itself. The
    one normalised string is what addresses a node in the backend, the index, and the UI.
    """
    parts: list[str] = []
    for seg in path.replace("\\", "/").split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            raise ValueError(f"path escapes the tenant root: {path!r}")
        parts.append(seg)
    return "/".join(parts)


class FileStore(ABC):
    """A tenant-scoped file space: read / write / list / delete behind one interface.

    Every method takes ``tenant`` explicitly (constraint #1) — the store never assumes a
    single global tenant. Concrete backends localise tenant scoping (a subdirectory for the
    local FS, a ``{tenant}-files`` bucket for S3) and own path-safety via :func:`normalize_rel`.
    Missing paths raise :class:`FileNotFoundError`; the platform API maps that to HTTP 404.
    """

    @abstractmethod
    async def read_bytes(self, *, tenant: str, path: str) -> bytes:
        """Return the raw bytes at *path*, or raise :class:`FileNotFoundError`."""

    @abstractmethod
    async def write_bytes(
        self, *, tenant: str, path: str, data: bytes, content_type: str | None = None
    ) -> FileEntry:
        """Write *data* at *path* (creating parents), returning the stored entry."""

    @abstractmethod
    async def list_dir(self, *, tenant: str, path: str = "") -> list[FileEntry]:
        """List the direct children of *path* (empty = tenant root); dirs before files."""

    @abstractmethod
    async def stat(self, *, tenant: str, path: str) -> FileEntry | None:
        """Return the entry at *path*, or ``None`` if it does not exist."""

    @abstractmethod
    async def delete(self, *, tenant: str, path: str) -> bool:
        """Delete the file or directory tree at *path*; return whether it existed.

        *path* must be non-empty — deleting the tenant root is rejected.
        """

    @abstractmethod
    async def ensure_dir(self, *, tenant: str, path: str) -> FileEntry:
        """Create the directory at *path* (and parents) if absent; return its entry."""

    # ── Concrete conveniences (in terms of the abstract byte API) ────────────────

    async def exists(self, *, tenant: str, path: str) -> bool:
        """Whether anything exists at *path*."""
        return (await self.stat(tenant=tenant, path=path)) is not None

    async def read_text(self, *, tenant: str, path: str) -> str:
        """Read a UTF-8 text file at *path*.

        Raises :class:`FileNotFoundError` (missing), :class:`ValueError` (larger than the
        256 KB text cap — stream the bytes instead), or :class:`UnicodeDecodeError` (binary).
        """
        data = await self.read_bytes(tenant=tenant, path=path)
        if len(data) > _TEXT_READ_MAX:
            raise ValueError(f"file is larger than {_TEXT_READ_MAX} bytes; read the bytes instead")
        return data.decode("utf-8")

    async def write_text(self, *, tenant: str, path: str, content: str) -> FileEntry:
        """Write UTF-8 *content* at *path* (creating parents), returning the stored entry."""
        return await self.write_bytes(
            tenant=tenant, path=path, data=content.encode("utf-8"), content_type="text/plain"
        )

    async def ensure_tenant_root(self, *, tenant: str) -> None:
        """Provision the tenant's root so later writes have a home (core-owned provisioning)."""
        await self.ensure_dir(tenant=tenant, path="")


class LocalFileStore(FileStore):
    """Local-filesystem backend (self-host): the tenant tree under ``<root>/<tenant>``.

    Blocking disk I/O is offloaded to a worker thread so the event loop stays free. Every
    resolved path is confined under the tenant root (``relative_to`` check), so a crafted
    key cannot escape even past :func:`normalize_rel`.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def _tenant_root(self, tenant: str) -> Path:
        return (self._root / validate_tenant_id(tenant)).resolve()

    def _abs(self, tenant: str, path: str) -> Path:
        base = self._tenant_root(tenant)
        resolved = (base / normalize_rel(path)).resolve()
        # Defence in depth: normalize_rel already rejects "..", but confirm containment in case
        # of a symlink inside the tree pointing outward.
        if resolved != base and base not in resolved.parents:
            raise ValueError(f"path escapes the tenant root: {path!r}")
        return resolved

    def _entry(self, tenant: str, target: Path) -> FileEntry:
        base = self._tenant_root(tenant)
        rel = "" if target == base else target.relative_to(base).as_posix()
        is_dir = target.is_dir()
        st = target.stat()
        return FileEntry(
            path=rel,
            name=target.name,
            kind="dir" if is_dir else "file",
            size=0 if is_dir else st.st_size,
            mtime=st.st_mtime,
        )

    async def read_bytes(self, *, tenant: str, path: str) -> bytes:
        target = self._abs(tenant, path)

        def _read() -> bytes:
            if not target.is_file():
                raise FileNotFoundError(path)
            return target.read_bytes()

        return await asyncio.to_thread(_read)

    async def write_bytes(
        self, *, tenant: str, path: str, data: bytes, content_type: str | None = None
    ) -> FileEntry:
        if not normalize_rel(path):
            raise ValueError("cannot write to the tenant root itself")
        target = self._abs(tenant, path)

        def _write() -> FileEntry:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            return self._entry(tenant, target)

        return await asyncio.to_thread(_write)

    async def list_dir(self, *, tenant: str, path: str = "") -> list[FileEntry]:
        target = self._abs(tenant, path)

        def _list() -> list[FileEntry]:
            if not target.is_dir():
                return []
            entries = [self._entry(tenant, child) for child in target.iterdir()]
            entries.sort(key=lambda e: (e.kind != "dir", e.name.lower()))
            return entries

        return await asyncio.to_thread(_list)

    async def stat(self, *, tenant: str, path: str) -> FileEntry | None:
        target = self._abs(tenant, path)

        def _stat() -> FileEntry | None:
            return self._entry(tenant, target) if target.exists() else None

        return await asyncio.to_thread(_stat)

    async def delete(self, *, tenant: str, path: str) -> bool:
        if not normalize_rel(path):
            raise ValueError("cannot delete the tenant root")
        target = self._abs(tenant, path)

        def _delete() -> bool:
            if target.is_dir():
                shutil.rmtree(target)
                return True
            if target.exists():
                target.unlink()
                return True
            return False

        return await asyncio.to_thread(_delete)

    async def ensure_dir(self, *, tenant: str, path: str) -> FileEntry:
        target = self._abs(tenant, path)

        def _mkdir() -> FileEntry:
            target.mkdir(parents=True, exist_ok=True)
            return self._entry(tenant, target)

        return await asyncio.to_thread(_mkdir)


class S3FileStore(FileStore):
    """S3 / MinIO backend (SaaS): the tenant tree as keys in a ``{tenant}-files`` bucket.

    Directories are virtual — listing uses a ``/`` delimiter so common prefixes appear as
    folders, exactly how object stores model a tree. ``aioboto3`` is imported lazily so the
    shared core library carries no hard S3 dependency; install the ``s3`` extra to use this.
    """

    def __init__(
        self, *, url: str, access_key: str, secret_key: str, bucket_base: str = "files"
    ) -> None:
        import aioboto3  # lazy: only the S3 backend needs the dependency

        self._url = url
        self._bucket_base = bucket_base
        self._session = aioboto3.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="us-east-1",
        )

    def _bucket(self, tenant: str) -> str:
        return scope_bucket(self._bucket_base, validate_tenant_id(tenant))

    @staticmethod
    def _mtime(obj: dict[str, Any]) -> float:
        last = obj.get("LastModified")
        return last.timestamp() if isinstance(last, datetime) else 0.0

    async def _ensure_bucket(self, s3: Any, bucket: str) -> None:
        from botocore.exceptions import ClientError

        try:
            await s3.head_bucket(Bucket=bucket)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"NoSuchBucket", "404"}:
                await s3.create_bucket(Bucket=bucket)
            else:
                raise

    async def read_bytes(self, *, tenant: str, path: str) -> bytes:
        from botocore.exceptions import ClientError

        key = normalize_rel(path)
        async with self._session.client("s3", endpoint_url=self._url) as s3:
            try:
                resp = await s3.get_object(Bucket=self._bucket(tenant), Key=key)
                return cast("bytes", await resp["Body"].read())
            except ClientError as exc:
                if exc.response["Error"]["Code"] in {"NoSuchKey", "NoSuchBucket", "404"}:
                    raise FileNotFoundError(path) from exc
                raise

    async def write_bytes(
        self, *, tenant: str, path: str, data: bytes, content_type: str | None = None
    ) -> FileEntry:
        key = normalize_rel(path)
        if not key:
            raise ValueError("cannot write to the tenant root itself")
        async with self._session.client("s3", endpoint_url=self._url) as s3:
            await self._ensure_bucket(s3, self._bucket(tenant))
            await s3.put_object(
                Bucket=self._bucket(tenant),
                Key=key,
                Body=data,
                ContentType=content_type or "application/octet-stream",
            )
        return FileEntry(path=key, name=key.rsplit("/", 1)[-1], kind="file", size=len(data))

    async def list_dir(self, *, tenant: str, path: str = "") -> list[FileEntry]:
        from botocore.exceptions import ClientError

        rel = normalize_rel(path)
        prefix = f"{rel}/" if rel else ""
        entries: list[FileEntry] = []
        async with self._session.client("s3", endpoint_url=self._url) as s3:
            try:
                paginator = s3.get_paginator("list_objects_v2")
                async for page in paginator.paginate(
                    Bucket=self._bucket(tenant), Prefix=prefix, Delimiter="/"
                ):
                    for cp in page.get("CommonPrefixes", []):
                        sub = cp["Prefix"][len(prefix) :].rstrip("/")
                        if sub:
                            entries.append(FileEntry(path=f"{prefix}{sub}", name=sub, kind="dir"))
                    for obj in page.get("Contents", []):
                        name = obj["Key"][len(prefix) :]
                        if name:  # skip the prefix placeholder itself
                            entries.append(
                                FileEntry(
                                    path=obj["Key"],
                                    name=name,
                                    kind="file",
                                    size=int(obj.get("Size", 0)),
                                    mtime=self._mtime(obj),
                                )
                            )
            except ClientError as exc:
                if exc.response["Error"]["Code"] in {"NoSuchBucket", "404"}:
                    return []
                raise
        entries.sort(key=lambda e: (e.kind != "dir", e.name.lower()))
        return entries

    async def stat(self, *, tenant: str, path: str) -> FileEntry | None:
        from botocore.exceptions import ClientError

        key = normalize_rel(path)
        if not key:
            return FileEntry(path="", name="", kind="dir")
        async with self._session.client("s3", endpoint_url=self._url) as s3:
            try:
                head = await s3.head_object(Bucket=self._bucket(tenant), Key=key)
                return FileEntry(
                    path=key,
                    name=key.rsplit("/", 1)[-1],
                    kind="file",
                    size=int(head.get("ContentLength", 0)),
                    mtime=self._mtime(head),
                )
            except ClientError as exc:
                if exc.response["Error"]["Code"] not in {"NoSuchKey", "NoSuchBucket", "404"}:
                    raise
        # Not an object — it may be a virtual directory (a prefix with children).
        children = await self.list_dir(tenant=tenant, path=key)
        return FileEntry(path=key, name=key.rsplit("/", 1)[-1], kind="dir") if children else None

    async def delete(self, *, tenant: str, path: str) -> bool:
        key = normalize_rel(path)
        if not key:
            raise ValueError("cannot delete the tenant root")
        bucket = self._bucket(tenant)
        async with self._session.client("s3", endpoint_url=self._url) as s3:
            # Delete the object itself and every key under it (a virtual directory tree).
            keys: list[dict[str, str]] = []
            paginator = s3.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=bucket, Prefix=key):
                for obj in page.get("Contents", []):
                    if obj["Key"] == key or obj["Key"].startswith(f"{key}/"):
                        keys.append({"Key": obj["Key"]})
            if not keys:
                return False
            # Delete one object per call: S3's batch DeleteObjects requires a Content-MD5
            # header that newer botocore no longer adds automatically, which MinIO rejects
            # (MissingContentMD5). Per-object deletes are portable and these trees are small.
            for obj in keys:
                await s3.delete_object(Bucket=bucket, Key=obj["Key"])
            return True

    async def ensure_dir(self, *, tenant: str, path: str) -> FileEntry:
        # Directories are implicit in S3 — there is nothing to create. Return the virtual node;
        # writing a file under the prefix is what makes it appear in a listing.
        rel = normalize_rel(path)
        return FileEntry(path=rel, name=rel.rsplit("/", 1)[-1] if rel else "", kind="dir")


def build_file_store(
    *,
    backend: FileStoreBackend = "local",
    root: str | Path = "/data",
    s3_url: str | None = None,
    s3_access_key: str | None = None,
    s3_secret_key: str | None = None,
) -> FileStore:
    """Construct the configured :class:`FileStore` — ``local`` (default) or ``s3``.

    The selection is the single swap point for dual-track constraint #3: the same contract
    serves a self-host filesystem and a SaaS object store, so no caller hardcodes a backend.
    """
    if backend == "s3":
        if not (s3_url and s3_access_key and s3_secret_key):
            raise ValueError("the s3 backend requires s3_url, s3_access_key and s3_secret_key")
        return S3FileStore(url=s3_url, access_key=s3_access_key, secret_key=s3_secret_key)
    return LocalFileStore(root)
