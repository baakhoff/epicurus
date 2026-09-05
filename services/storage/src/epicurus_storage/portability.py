"""Tenant portability for the storage module — the one module whose data is *bytes* (#876).

Every other portable module exports rows. Storage exports two things that only mean something
together: the **catalogue** (the ``storage_files`` rows that make an object browsable,
searchable and downloadable) and the **objects** themselves, which live in the tenant's own
bucket and are therefore *not* in the core-owned file space the archive already carries. Lose
the bytes and an operator moves house to a list of filenames; lose the rows and the bytes are
in a bucket nothing can see (#347's lesson, in reverse).

So this store implements both halves of the contract:

* :class:`~epicurus_core.PortabilityStore` — one record per catalogue entry, id = its path.
* :class:`~epicurus_core.BlobPortabilityStore` — one blob per object, **id = the same path**,
  so a record and its bytes pair up with no join table and no second vocabulary.

What travels is only what this module owns: ``source="object"`` rows. The legacy
``source="fs"`` rows mirror the core's file space (ADR-0063) and are excluded as derived — the
core exports that tree itself.

Two rules give the byte half the same guarantees the row half has. **Idempotency is by
content**: a blob whose id already holds bytes with the same SHA-256 is skipped, so a second
apply writes nothing. **Nothing is ever overwritten**: an id holding *different* bytes keeps
them and is named as a conflict, exactly as the core treats a file-space file the operator has
since edited. Between those two, applying an archive twice — or into a populated install — can
add but never destroy.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator

from epicurus_core import (
    BlobOutcome,
    BlobRef,
    ImportOutcome,
    ImportReport,
    PortabilityRecord,
    get_logger,
)
from epicurus_storage.db import FileEntry, FileIndex
from epicurus_storage.object_store import ObjectStore
from epicurus_storage.service import MODULE_NAME, _normalize_key

log = get_logger(f"{MODULE_NAME}.portability")

SCHEMA = "storage/1"
"""The module's record schema. Bumped only when a record's *shape* changes."""

FILE_KIND = "file"
FOLDER_KIND = "folder"
"""The two record kinds: an object-backed file entry, and a folder row that makes it reachable.

Folders travel as records of their own rather than being re-derived from the file paths on
import. Deriving them would be nearly right and quietly wrong: a folder the operator created
and then emptied has no file under it to derive it from, and would silently not survive the
move.
"""

# Read an existing object in chunks when hashing it for the same-bytes check — the whole point
# of the blob half is that no object is ever held whole.
_HASH_CHUNK_BYTES = 1024 * 1024

_PAGE = 500


class StoragePortability:
    """The storage module's portability store — catalogue records plus object bytes."""

    def __init__(self, index: FileIndex, objects: ObjectStore) -> None:
        self._index = index
        self._objects = objects

    @property
    def schema(self) -> str:
        return SCHEMA

    # ── records: the catalogue ───────────────────────────────────────────────

    async def export(self, *, tenant_id: str) -> AsyncIterator[PortabilityRecord]:
        """Stream one record per object-backed catalogue entry, ordered by path."""
        async for entry in self._entries(tenant_id):
            yield _record(entry)

    async def import_(
        self,
        *,
        tenant_id: str,
        records: AsyncIterator[PortabilityRecord],
        dry_run: bool,
    ) -> ImportReport:
        """Upsert catalogue entries by path — additively, never deleting."""
        report = ImportReport(schema_name=SCHEMA)
        async for record in records:
            if record.kind not in (FILE_KIND, FOLDER_KIND):
                report.record(record.kind, "skipped")
                report.warn(f"unknown record kind {record.kind!r} was skipped")
                continue
            outcome = await self._upsert(tenant_id, record, dry_run=dry_run)
            report.record(record.kind, outcome)
        return report

    async def _upsert(
        self, tenant: str, record: PortabilityRecord, *, dry_run: bool
    ) -> ImportOutcome:
        """Apply one catalogue record; the same decision a dry run only counts."""
        path = _normalize_key(record.id)
        if not path:
            return "skipped"
        kind = "dir" if record.kind == FOLDER_KIND else "file"
        existing = await self._index.get(tenant=tenant, path=path)
        if existing is not None and existing.source != "object":
            # A read-only row this module does not own (the legacy filesystem mirror). Import
            # is additive *and* stays inside its own lane: nothing here rewrites another
            # owner's entry, even to a value that looks better.
            return "skipped"
        row: dict[str, object] = {
            "path": path,
            "name": str(record.data.get("name") or path.rsplit("/", 1)[-1]),
            "size": int(record.data.get("size") or 0),
            "mtime": float(record.data.get("mtime") or 0.0),
            "kind": kind,
        }
        if existing is not None and _unchanged(existing, row):
            return "skipped"
        if not dry_run:
            await self._index.upsert_batch(tenant=tenant, source="object", entries=[row])
        return "updated" if existing is not None else "created"

    # ── blobs: the objects themselves ────────────────────────────────────────

    async def blobs(self, *, tenant_id: str) -> AsyncIterator[BlobRef]:
        """List every object this tenant's catalogue points at, with its size and media type.

        Driven by the catalogue rather than by a bucket listing: the index is what the Files
        page, the download and the agent's tools all read, so an object with no row is
        unreachable here and would arrive at the far end just as unreachable. A row whose
        bytes are *gone* is likewise not listed — the record still travels, so the entry lands
        exactly as it stands here.
        """
        async for entry in self._entries(tenant_id):
            if entry.kind != "file":
                continue
            stat = await self._objects.stat_object(tenant=tenant_id, key=entry.path)
            if stat is None:
                log.warning("catalogued object has no bytes; not exported", path=entry.path)
                continue
            yield BlobRef(id=entry.path, size=stat.size, content_type=stat.content_type)

    async def open_blob(self, *, tenant_id: str, blob_id: str) -> AsyncIterator[bytes]:
        """Stream one object's bytes straight off the backend."""
        key = _normalize_key(blob_id)
        if not key:
            raise FileNotFoundError(blob_id)
        async for chunk in self._objects.open_object(tenant=tenant_id, key=key):
            yield chunk

    async def put_blob(
        self,
        *,
        tenant_id: str,
        blob_id: str,
        sha256: str,
        size: int,
        content_type: str,
        chunks: AsyncIterator[bytes],
    ) -> BlobOutcome:
        """Write one object — by content, and never over anything that is already there."""
        key = _normalize_key(blob_id)
        if not key:
            await _drain(chunks)
            return BlobOutcome(id=blob_id, outcome="skipped", warning="unusable object id")
        existing = await self._objects.stat_object(tenant=tenant_id, key=key)
        if existing is not None:
            # Already occupied. Decide by digest, and either way write nothing: the incoming
            # stream is still drained so the transfer completes (and so the library's own
            # verification still runs on it) rather than dying half-read on the wire.
            here = await self._digest(tenant_id, key)
            await _drain(chunks)
            if sha256 and here == sha256:
                return BlobOutcome(id=key, outcome="skipped")
            return BlobOutcome(
                id=key,
                outcome="skipped",
                warning=f"{key} already holds different bytes and was left untouched",
            )
        await self._objects.put_stream(
            tenant=tenant_id, key=key, chunks=chunks, content_type=content_type
        )
        return BlobOutcome(id=key, outcome="created")

    async def _digest(self, tenant: str, key: str) -> str:
        """SHA-256 of the object already at *key*, streamed rather than loaded."""
        digest = hashlib.sha256()
        async for chunk in self._objects.open_object(
            tenant=tenant, key=key, chunk_size=_HASH_CHUNK_BYTES
        ):
            digest.update(chunk)
        return digest.hexdigest()

    # ── shared ───────────────────────────────────────────────────────────────

    async def _entries(self, tenant: str) -> AsyncIterator[FileEntry]:
        """Every object-backed catalogue entry, a page at a time, ordered by path."""
        after = ""
        while True:
            page = await self._index.object_entries(tenant=tenant, after=after, limit=_PAGE)
            if not page:
                return
            for entry in page:
                yield entry
            after = page[-1].path


def _record(entry: FileEntry) -> PortabilityRecord:
    """One catalogue row as a record: its path as the stable id, its own columns as data.

    ``tenant`` never travels (the target re-applies its own), the surrogate ``id`` never
    travels (the path is the natural key), ``source`` never travels (everything exported here
    is an object row, and the import writes it back as one), and ``updated_at`` never travels
    — it records when *this* installation last wrote the row, which says nothing about the
    file and would only make an unchanged re-import look like a change.
    """
    return PortabilityRecord(
        kind=FOLDER_KIND if entry.kind == "dir" else FILE_KIND,
        id=entry.path,
        data={"name": entry.name, "size": entry.size, "mtime": entry.mtime},
    )


def _unchanged(existing: FileEntry, row: dict[str, object]) -> bool:
    """Whether an incoming row says anything the stored one does not already say."""
    return (
        existing.name == row["name"]
        and existing.size == row["size"]
        and existing.mtime == row["mtime"]
        and existing.kind == row["kind"]
    )


async def _drain(chunks: AsyncIterator[bytes]) -> None:
    """Read an incoming body to its end and discard it."""
    async for _ in chunks:
        pass
