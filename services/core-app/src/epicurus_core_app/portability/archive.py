"""Reading and writing the tenant archive — a plain ``.tar.gz`` with a documented layout (#867).

::

    manifest.json                 what this archive is, and what it deliberately omits
    core/<set>.ndjson             the core's own data, one set per member
    modules/<name>.ndjson         each portable module's stream, verbatim
    modules/<name>/blobs.ndjson   that module's blob listing, verbatim (#876)
    modules/<name>/blobs/<id>     one member per blob — the bytes themselves
    files/<path>                  the tenant file space, paths relative to the tenant root

Deliberately boring: gzip and tar, JSON and newline-delimited JSON. An operator can open
this with tools they already have, and *read* what they are about to move — which matters
more here than any saving a bespoke format would buy.

Everything blocking (gzip, tar, filesystem) runs on a worker thread. The export and the
apply are background jobs on the same event loop that serves chat, and a multi-gigabyte
archive must not stall a turn.
"""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
import time
from collections.abc import AsyncIterator
from pathlib import Path
from types import TracebackType
from typing import Any

from epicurus_core import BlobRef, PortabilityRecord, StreamHeader, get_logger
from epicurus_core_app.portability.models import (
    ARCHIVE_MANIFEST_MEMBER,
    CORE_MEMBER_PREFIX,
    FILES_MEMBER_PREFIX,
    MODULE_MEMBER_PREFIX,
    ArchiveManifest,
)

__all__ = [
    "ArchiveReader",
    "ArchiveWriter",
    "MemberError",
    "core_member",
    "files_member",
    "module_blob_member",
    "module_blob_prefix",
    "module_blobs_member",
    "module_member",
    "sanitize_member",
]

log = get_logger("core.portability.archive")

# Read the compressed stream in chunks rather than a line at a time: a hop to the worker
# thread per line would cost more than the parse does.
_CHUNK = 256 * 1024


class MemberError(ValueError):
    """An archive member that cannot be trusted, or a member that should be there and is not."""


def core_member(set_name: str) -> str:
    return f"{CORE_MEMBER_PREFIX}{set_name}.ndjson"


def module_member(name: str) -> str:
    return f"{MODULE_MEMBER_PREFIX}{name}.ndjson"


def files_member(relative_path: str) -> str:
    return f"{FILES_MEMBER_PREFIX}{relative_path}"


def module_blobs_member(name: str) -> str:
    """Where a module's blob *listing* lives — one :class:`BlobRef` per line (#876)."""
    return f"{MODULE_MEMBER_PREFIX}{name}/blobs.ndjson"


def module_blob_prefix(name: str) -> str:
    """The member prefix every one of a module's blobs sits under."""
    return f"{MODULE_MEMBER_PREFIX}{name}/blobs/"


def module_blob_member(name: str, blob_id: str) -> str:
    """Where one blob's bytes live — the id kept verbatim, so the layout stays readable."""
    return f"{module_blob_prefix(name)}{blob_id}"


def sanitize_member(name: str) -> str | None:
    """Return *name* if it is a member we are willing to touch, else ``None``.

    Tar is a format where a member may be named ``../../etc/passwd`` or ``/etc/passwd``, and
    a naive extractor writes exactly there ("tar slip"). Nothing in this codebase extracts
    to a path built from a member name — the file half is written through the
    :class:`~epicurus_core.files.FileStore` API, which refuses an escape of its own — but the
    check belongs at the door as well as the destination: an uploaded archive is operator
    input, and one guard that has to hold is worse than two.

    Also rejects anything outside the four known prefixes, so an archive cannot smuggle in a
    member the reader would otherwise have to have an opinion about.
    """
    cleaned = name.replace("\\", "/").strip()
    if not cleaned or cleaned.startswith("/"):
        return None
    parts = cleaned.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    if cleaned == ARCHIVE_MANIFEST_MEMBER:
        return cleaned
    known = (CORE_MEMBER_PREFIX, MODULE_MEMBER_PREFIX, FILES_MEMBER_PREFIX)
    return cleaned if cleaned.startswith(known) else None


class ArchiveWriter:
    """Builds one ``.tar.gz`` at *path*, member by member, off the event loop."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._tar: tarfile.TarFile | None = None

    async def __aenter__(self) -> ArchiveWriter:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._tar = await asyncio.to_thread(tarfile.open, self._path, "w:gz")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        tar, self._tar = self._tar, None
        if tar is not None:
            await asyncio.to_thread(tar.close)

    def _require(self) -> tarfile.TarFile:
        if self._tar is None:
            raise RuntimeError("archive writer is not open")
        return self._tar

    async def add_bytes(self, member: str, data: bytes) -> None:
        """Write *data* as *member* (used for ``manifest.json`` and for each file's bytes)."""
        tar = self._require()
        info = tarfile.TarInfo(name=member)
        info.size = len(data)
        info.mtime = int(time.time())
        await asyncio.to_thread(tar.addfile, info, io.BytesIO(data))

    async def add_path(self, member: str, source: Path) -> None:
        """Write the staged file at *source* as *member*, streamed by tarfile itself."""
        tar = self._require()
        await asyncio.to_thread(tar.add, str(source), member)


class ArchiveReader:
    """Reads a staged ``.tar.gz``: its manifest, its NDJSON members, and its files."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._tar: tarfile.TarFile | None = None
        self._members: dict[str, tarfile.TarInfo] = {}
        self._rejected: list[str] = []

    async def __aenter__(self) -> ArchiveReader:
        try:
            self._tar = await asyncio.to_thread(tarfile.open, self._path, "r:gz")
        except tarfile.TarError as exc:
            raise MemberError(f"not a readable .tar.gz archive: {exc}") from exc
        for info in await asyncio.to_thread(self._tar.getmembers):
            if not info.isfile():
                continue
            safe = sanitize_member(info.name)
            if safe is None:
                self._rejected.append(info.name)
                continue
            self._members[safe] = info
        if self._rejected:
            log.warning(
                "portability archive contained unsafe member names; ignored",
                members=self._rejected[:10],
                count=len(self._rejected),
            )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        tar, self._tar = self._tar, None
        if tar is not None:
            await asyncio.to_thread(tar.close)

    @property
    def rejected_members(self) -> list[str]:
        """Member names refused by :func:`sanitize_member` — reported, never read."""
        return list(self._rejected)

    def has(self, member: str) -> bool:
        return member in self._members

    def ndjson_members(self, prefix: str) -> list[str]:
        """Every component ``.ndjson`` member **directly** under *prefix*, in archive order.

        "Directly" is load-bearing since blobs arrived (#876): a module's own bytes live at
        ``modules/<name>/blobs/<id>``, and a stored file legitimately called ``notes.ndjson``
        would otherwise be read back as if it were the export stream of a module named
        ``<name>/blobs/notes``. A component member has exactly one path segment after its
        prefix; anything deeper belongs to that component, not beside it.
        """
        return [
            name
            for name in self._members
            if name.startswith(prefix)
            and name.endswith(".ndjson")
            and "/" not in name[len(prefix) :]
        ]

    def blob_members(self, module_name: str) -> list[tuple[str, int]]:
        """``(blob id, size)`` for every blob member of *module_name*, in archive order."""
        prefix = module_blob_prefix(module_name)
        return [
            (name[len(prefix) :], self._members[name].size)
            for name in sorted(self._members)
            if name.startswith(prefix)
        ]

    def file_members(self) -> list[tuple[str, int]]:
        """``(path relative to the tenant root, size)`` for every member under ``files/``."""
        return [
            (name[len(FILES_MEMBER_PREFIX) :], self._members[name].size)
            for name in sorted(self._members)
            if name.startswith(FILES_MEMBER_PREFIX)
        ]

    async def read_bytes(self, member: str) -> bytes:
        """The whole of one member — used for ``manifest.json`` and for a single file."""
        tar = self._require()
        info = self._members.get(member)
        if info is None:
            raise MemberError(f"archive has no member {member!r}")

        def _read() -> bytes:
            handle = tar.extractfile(info)
            if handle is None:
                raise MemberError(f"member {member!r} is not a regular file")
            with handle:
                return handle.read()

        return await asyncio.to_thread(_read)

    async def manifest(self) -> ArchiveManifest:
        """The archive's ``manifest.json``, validated."""
        try:
            payload: Any = json.loads(await self.read_bytes(ARCHIVE_MANIFEST_MEMBER))
        except (MemberError, ValueError) as exc:
            raise MemberError(f"archive has no readable {ARCHIVE_MANIFEST_MEMBER}: {exc}") from exc
        try:
            return ArchiveManifest.model_validate(payload)
        except Exception as exc:
            raise MemberError(f"malformed {ARCHIVE_MANIFEST_MEMBER}: {exc}") from exc

    async def lines(self, member: str) -> AsyncIterator[str]:
        """Non-empty lines of one NDJSON member, re-assembled across read boundaries."""
        tar = self._require()
        info = self._members.get(member)
        if info is None:
            raise MemberError(f"archive has no member {member!r}")
        handle = await asyncio.to_thread(tar.extractfile, info)
        if handle is None:
            raise MemberError(f"member {member!r} is not a regular file")
        try:
            buffer = b""
            while True:
                chunk = await asyncio.to_thread(handle.read, _CHUNK)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    raw, _, buffer = buffer.partition(b"\n")
                    line = raw.strip()
                    if line:
                        yield line.decode("utf-8")
            tail = buffer.strip()
            if tail:
                yield tail.decode("utf-8")
        finally:
            await asyncio.to_thread(handle.close)

    async def raw_lines(self, member: str) -> AsyncIterator[bytes]:
        """The member's bytes, chunk by chunk — the module import body, unaltered.

        Deliberately *not* re-serialized from parsed records: the module must receive the
        stream the source module wrote, header line and all, so that it applies its own
        compatibility rule to the real thing rather than to the core's paraphrase of it.

        The same reader serves a blob member (which is bytes, not lines); the name is kept
        for its callers.
        """
        tar = self._require()
        info = self._members.get(member)
        if info is None:
            raise MemberError(f"archive has no member {member!r}")
        handle = await asyncio.to_thread(tar.extractfile, info)
        if handle is None:
            raise MemberError(f"member {member!r} is not a regular file")
        try:
            while True:
                chunk = await asyncio.to_thread(handle.read, _CHUNK)
                if not chunk:
                    break
                yield chunk
        finally:
            await asyncio.to_thread(handle.close)

    async def header(self, member: str) -> StreamHeader | None:
        """The member's header line, or ``None`` if it has none (an empty component)."""
        async for line in self.lines(member):
            try:
                return StreamHeader.model_validate_json(line)
            except Exception as exc:
                raise MemberError(f"{member}: malformed header line: {exc}") from exc
        return None

    async def records(self, member: str) -> AsyncIterator[PortabilityRecord]:
        """Every record of one NDJSON member (the header line consumed and discarded)."""
        first = True
        number = 0
        async for line in self.lines(member):
            number += 1
            if first:
                first = False
                continue
            try:
                yield PortabilityRecord.model_validate_json(line)
            except Exception as exc:
                raise MemberError(f"{member}: malformed record on line {number}: {exc}") from exc

    async def blob_refs(self, member: str) -> AsyncIterator[BlobRef]:
        """Every :class:`BlobRef` of a blob listing member — no header line to skip (#876).

        The listing travels verbatim, so it names blobs the archive may not actually carry:
        an object over the per-blob export ceiling is listed and omitted, deliberately, so
        the import can say which bytes are missing instead of leaving it to be inferred.
        """
        number = 0
        async for line in self.lines(member):
            number += 1
            try:
                yield BlobRef.model_validate_json(line)
            except Exception as exc:
                raise MemberError(f"{member}: malformed blob ref on line {number}: {exc}") from exc

    async def count_records(self, member: str) -> int:
        """How many records one member carries (its lines, less the header)."""
        total = 0
        async for _ in self.lines(member):
            total += 1
        return max(total - 1, 0)

    async def count_lines(self, member: str) -> int:
        """How many non-empty lines a member has — a blob listing carries no header."""
        total = 0
        async for _ in self.lines(member):
            total += 1
        return total

    def _require(self) -> tarfile.TarFile:
        if self._tar is None:
            raise RuntimeError("archive reader is not open")
        return self._tar
