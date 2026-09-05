"""Tenant portability for storage (#876) — the catalogue as records, the objects as blobs.

Storage is the one module whose export is *bytes*, so the round trip that matters is not
"records came back" but "the file is there and opens": export → wipe both halves → import →
byte-for-byte equal. Everything runs against an in-memory object store (the real MinIO round
trip lives, under the integration marker, in ``test_storage_objects.py``) and a file-backed
SQLite index, so nothing here needs infra.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_core import (
    BlobPortabilityStore,
    BlobRef,
    EpicurusModule,
    PortabilityRecord,
    PortabilityStore,
    add_portability_routes,
    verified_chunks,
)
from epicurus_storage.db import FileIndex
from epicurus_storage.object_store import ObjectStat, ObjectStore, StoredObject
from epicurus_storage.portability import SCHEMA, StoragePortability
from epicurus_storage.service import ingest_object, put_object

TENANT = "local"
OTHER = "elsewhere"


class MemObjectStore(ObjectStore):
    """In-memory object store covering the whole surface portability uses."""

    def __init__(self) -> None:
        super().__init__(url="http://unused", access_key="x", secret_key="x")
        self.mem: dict[tuple[str, str], StoredObject] = {}

    async def put_bytes(
        self, *, tenant: str, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> None:
        self.mem[(tenant, key)] = StoredObject(data=data, content_type=content_type)

    async def get_object(self, *, tenant: str, key: str) -> StoredObject | None:
        return self.mem.get((tenant, key))

    async def stat_object(self, *, tenant: str, key: str) -> ObjectStat | None:
        stored = self.mem.get((tenant, key))
        return None if stored is None else ObjectStat(len(stored.data), stored.content_type)

    async def open_object(
        self, *, tenant: str, key: str, chunk_size: int = 1024
    ) -> AsyncIterator[bytes]:
        stored = self.mem.get((tenant, key))
        if stored is None:
            raise FileNotFoundError(key)
        for start in range(0, len(stored.data), chunk_size):
            yield stored.data[start : start + chunk_size]

    async def put_stream(
        self,
        *,
        tenant: str,
        key: str,
        chunks: AsyncIterator[bytes],
        content_type: str = "application/octet-stream",
    ) -> int:
        # Mirrors the real one's contract: nothing is published until the last chunk has
        # arrived, so a stream that fails verification never becomes an object.
        buffer = bytearray()
        async for chunk in chunks:
            buffer += chunk
        self.mem[(tenant, key)] = StoredObject(data=bytes(buffer), content_type=content_type)
        return len(buffer)

    async def delete(self, *, tenant: str, key: str) -> None:
        self.mem.pop((tenant, key), None)


@pytest.fixture
async def index(tmp_path: Path) -> AsyncIterator[FileIndex]:
    """File-backed SQLite (never in-memory + StaticPool) with a disposed engine after."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'index.db'}")
    idx = FileIndex(engine)
    await idx.init()
    try:
        yield idx
    finally:
        await engine.dispose()


@pytest.fixture
def objects() -> MemObjectStore:
    return MemObjectStore()


@pytest.fixture
def store(index: FileIndex, objects: MemObjectStore) -> StoragePortability:
    return StoragePortability(index, objects)


def _client(store: StoragePortability) -> AsyncClient:
    module = EpicurusModule("storage", version="0.10.0", portable=True)
    app = FastAPI()
    add_portability_routes(app, module, store)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://storage")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def _records(store: StoragePortability, tenant: str = TENANT) -> list[PortabilityRecord]:
    return [r async for r in store.export(tenant_id=tenant)]


async def _refs(store: StoragePortability, tenant: str = TENANT) -> list[BlobRef]:
    return [r async for r in store.blobs(tenant_id=tenant)]


async def _stream(records: list[PortabilityRecord]) -> AsyncIterator[PortabilityRecord]:
    for record in records:
        yield record


# ── the module satisfies both halves of the contract ──────────────────────────


def test_the_store_is_both_a_record_store_and_a_blob_store(store: StoragePortability) -> None:
    assert isinstance(store, PortabilityStore)
    assert isinstance(store, BlobPortabilityStore)
    assert store.schema == SCHEMA


async def test_the_module_declares_portable_and_serves_all_five_routes(
    store: StoragePortability,
) -> None:
    from epicurus_core import route_paths
    from epicurus_storage.service import build_module

    module = build_module(
        FileIndex(create_async_engine("sqlite+aiosqlite:///:memory:")),
        MemObjectStore(),
        platform=None,  # type: ignore[arg-type]
        tenant=TENANT,
    )
    assert (await module.manifest()).portable is True

    app = FastAPI()
    add_portability_routes(app, module, store)
    paths = route_paths(app)
    assert "/export" in paths
    assert "/import" in paths
    assert "/export/blobs" in paths
    # ``:path`` and not ``{blob_id}``: an object id is a whole path (``uploads/a/b.pdf``), so
    # a single-segment converter would 404 on every nested key the module actually stores.
    assert "/export/blobs/{blob_id:path}" in paths
    assert "/import/blobs/{blob_id:path}" in paths


# ── what travels, and what does not ───────────────────────────────────────────


async def test_records_carry_the_catalogue_and_never_the_tenant_or_surrogate_id(
    index: FileIndex, objects: MemObjectStore, store: StoragePortability
) -> None:
    await put_object(index=index, objects=objects, tenant=TENANT, key="reports/q2.md", content="hi")
    records = await _records(store)

    assert [(r.kind, r.id) for r in records] == [
        ("folder", "reports"),
        ("file", "reports/q2.md"),
    ]
    payload = records[1].data
    assert payload == {"name": "q2.md", "size": 2, "mtime": 0.0}
    for record in records:
        assert "tenant" not in record.data
        assert "id" not in record.data
        assert "source" not in record.data
        assert "updated_at" not in record.data


async def test_the_filesystem_mirror_rows_are_excluded_as_derived(
    index: FileIndex, objects: MemObjectStore, store: StoragePortability
) -> None:
    """``source="fs"`` mirrors the core-owned file space (ADR-0063), which travels in
    ``files/``. Carrying it here would export the same tree twice, as this module's."""
    await put_object(index=index, objects=objects, tenant=TENANT, key="mine.txt", content="mine")
    await index.upsert_batch(
        tenant=TENANT,
        source="fs",
        entries=[
            {"path": "scanned.txt", "name": "scanned.txt", "size": 4, "mtime": 1.0, "kind": "file"}
        ],
    )
    assert [r.id for r in await _records(store)] == ["mine.txt"]
    assert [r.id for r in await _refs(store)] == ["mine.txt"]


async def test_a_catalogued_row_whose_bytes_are_gone_is_listed_but_carries_no_blob(
    index: FileIndex, objects: MemObjectStore, store: StoragePortability
) -> None:
    await put_object(index=index, objects=objects, tenant=TENANT, key="orphan.md", content="x")
    await objects.delete(tenant=TENANT, key="orphan.md")
    assert [r.id for r in await _records(store)] == ["orphan.md"]
    assert await _refs(store) == []


async def test_blob_refs_describe_the_objects_with_their_stored_media_type(
    index: FileIndex, objects: MemObjectStore, store: StoragePortability
) -> None:
    await ingest_object(
        index=index,
        objects=objects,
        tenant=TENANT,
        att_id="a1",
        filename="scan.pdf",
        content_type="application/pdf",
        data=b"%PDF-1.4 body",
    )
    refs = await _refs(store)
    assert [r.id for r in refs] == ["uploads/a1-scan.pdf"]
    assert refs[0].size == len(b"%PDF-1.4 body")
    assert refs[0].content_type == "application/pdf"


async def test_export_is_tenant_scoped_on_both_halves(
    index: FileIndex, objects: MemObjectStore, store: StoragePortability
) -> None:
    await put_object(index=index, objects=objects, tenant=TENANT, key="mine.md", content="mine")
    await put_object(index=index, objects=objects, tenant=OTHER, key="theirs.md", content="theirs")
    assert [r.id for r in await _records(store)] == ["mine.md"]
    assert [r.id for r in await _refs(store)] == ["mine.md"]
    assert [r.id for r in await _records(store, OTHER)] == ["theirs.md"]


async def test_the_export_pages_through_a_catalogue_larger_than_one_page(
    index: FileIndex, objects: MemObjectStore, store: StoragePortability
) -> None:
    """The keyset pager must not stop at its own page size, and must not repeat a row."""
    await index.upsert_batch(
        tenant=TENANT,
        source="object",
        entries=[
            {
                "path": f"bulk/{i:04d}.txt",
                "name": f"{i:04d}.txt",
                "size": 1,
                "mtime": 0.0,
                "kind": "file",
            }
            for i in range(1200)
        ],
    )
    ids = [r.id for r in await _records(store)]
    assert len(ids) == 1200
    assert len(set(ids)) == 1200
    assert ids == sorted(ids)


# ── the round trip ────────────────────────────────────────────────────────────


async def test_export_wipe_import_restores_the_rows_and_the_bytes(
    index: FileIndex, objects: MemObjectStore, store: StoragePortability
) -> None:
    await ingest_object(
        index=index,
        objects=objects,
        tenant=TENANT,
        att_id="a1",
        filename="photo.png",
        content_type="image/png",
        data=b"\x89PNG\r\n\x1a\n binary bytes \x00\xff",
    )
    await put_object(index=index, objects=objects, tenant=TENANT, key="notes/plan.md", content="p")
    before_records = await _records(store)
    before_refs = await _refs(store)
    payloads = {
        ref.id: b"".join([c async for c in store.open_blob(tenant_id=TENANT, blob_id=ref.id)])
        for ref in before_refs
    }

    # Wipe both halves — a fresh installation, not a merge.
    await index.delete_subtree(tenant=TENANT, path="uploads")
    await index.delete_subtree(tenant=TENANT, path="notes")
    objects.mem.clear()
    assert await _records(store) == []

    report = await store.import_(tenant_id=TENANT, records=_stream(before_records), dry_run=False)
    for ref in before_refs:
        outcome = await store.put_blob(
            tenant_id=TENANT,
            blob_id=ref.id,
            sha256=_sha(payloads[ref.id]),
            size=ref.size,
            content_type=ref.content_type,
            chunks=_chunks(payloads[ref.id]),
        )
        assert outcome.outcome == "created"

    assert report.counts["file"].created == 2
    assert report.counts["folder"].created == 2
    assert await _records(store) == before_records
    assert await _refs(store) == before_refs
    for ref in before_refs:
        stored = await objects.get_object(tenant=TENANT, key=ref.id)
        assert stored is not None
        assert stored.data == payloads[ref.id]
        assert stored.content_type == ref.content_type


async def test_a_second_apply_changes_nothing(
    index: FileIndex, objects: MemObjectStore, store: StoragePortability
) -> None:
    await put_object(index=index, objects=objects, tenant=TENANT, key="a/b.md", content="body")
    records = await _records(store)
    payload = b"body"

    report = await store.import_(tenant_id=TENANT, records=_stream(records), dry_run=False)
    outcome = await store.put_blob(
        tenant_id=TENANT,
        blob_id="a/b.md",
        sha256=_sha(payload),
        size=len(payload),
        content_type="text/plain",
        chunks=_chunks(payload),
    )
    assert report.total == 2
    assert all(c.created == 0 and c.updated == 0 for c in report.counts.values())
    assert outcome.outcome == "skipped"
    assert outcome.warning is None
    assert await _records(store) == records


async def test_an_id_holding_different_bytes_is_a_conflict_and_is_left_alone(
    index: FileIndex, objects: MemObjectStore, store: StoragePortability
) -> None:
    await put_object(index=index, objects=objects, tenant=TENANT, key="a.md", content="mine now")
    incoming = b"the archive's older copy"
    outcome = await store.put_blob(
        tenant_id=TENANT,
        blob_id="a.md",
        sha256=_sha(incoming),
        size=len(incoming),
        content_type="text/plain",
        chunks=_chunks(incoming),
    )
    assert outcome.outcome == "skipped"
    assert outcome.warning is not None
    assert "a.md" in outcome.warning
    stored = await objects.get_object(tenant=TENANT, key="a.md")
    assert stored is not None
    assert stored.data == b"mine now"


async def test_a_dry_run_counts_exactly_what_an_apply_would_do_and_writes_nothing(
    index: FileIndex, objects: MemObjectStore, store: StoragePortability
) -> None:
    records = [
        PortabilityRecord(kind="folder", id="docs", data={"name": "docs"}),
        PortabilityRecord(kind="file", id="docs/a.md", data={"name": "a.md", "size": 3}),
    ]
    dry = await store.import_(tenant_id=TENANT, records=_stream(records), dry_run=True)
    assert await index.get(tenant=TENANT, path="docs/a.md") is None

    wet = await store.import_(tenant_id=TENANT, records=_stream(records), dry_run=False)
    assert dry.model_dump() == wet.model_dump()
    assert await index.get(tenant=TENANT, path="docs/a.md") is not None


async def test_an_updated_row_is_counted_as_updated_and_an_identical_one_as_skipped(
    index: FileIndex, objects: MemObjectStore, store: StoragePortability
) -> None:
    await put_object(index=index, objects=objects, tenant=TENANT, key="a.md", content="x")
    same = PortabilityRecord(kind="file", id="a.md", data={"name": "a.md", "size": 1, "mtime": 0.0})
    changed = PortabilityRecord(kind="file", id="a.md", data={"name": "renamed.md", "size": 9})

    first = await store.import_(tenant_id=TENANT, records=_stream([same]), dry_run=False)
    second = await store.import_(tenant_id=TENANT, records=_stream([changed]), dry_run=False)
    assert first.counts["file"].skipped == 1
    assert second.counts["file"].updated == 1
    entry = await index.get(tenant=TENANT, path="a.md")
    assert entry is not None
    assert entry.name == "renamed.md"


async def test_an_unknown_kind_is_skipped_with_a_warning(store: StoragePortability) -> None:
    records = [PortabilityRecord(kind="symlink", id="x", data={})]
    report = await store.import_(tenant_id=TENANT, records=_stream(records), dry_run=False)
    assert report.counts["symlink"].skipped == 1
    assert any("symlink" in w for w in report.warnings)


async def test_an_import_never_rewrites_a_row_this_module_does_not_own(
    index: FileIndex, store: StoragePortability
) -> None:
    """A read-only ``fs`` row belongs to whatever put it there; import stays in its lane."""
    await index.upsert_batch(
        tenant=TENANT,
        source="fs",
        entries=[
            {"path": "shared.txt", "name": "shared.txt", "size": 4, "mtime": 7.0, "kind": "file"}
        ],
    )
    records = [
        PortabilityRecord(kind="file", id="shared.txt", data={"name": "hijacked", "size": 1})
    ]
    report = await store.import_(tenant_id=TENANT, records=_stream(records), dry_run=False)
    assert report.counts["file"].skipped == 1
    entry = await index.get(tenant=TENANT, path="shared.txt")
    assert entry is not None
    assert entry.name == "shared.txt"
    assert entry.source == "fs"


async def test_an_import_lands_in_the_target_tenant_not_the_source_one(
    index: FileIndex, store: StoragePortability
) -> None:
    records = [PortabilityRecord(kind="file", id="a.md", data={"name": "a.md", "size": 1})]
    await store.import_(tenant_id=OTHER, records=_stream(records), dry_run=False)
    assert await index.get(tenant=TENANT, path="a.md") is None
    assert await index.get(tenant=OTHER, path="a.md") is not None


# ── over the routes ───────────────────────────────────────────────────────────


async def test_the_routes_round_trip_a_binary_object_end_to_end(
    index: FileIndex, objects: MemObjectStore, store: StoragePortability
) -> None:
    payload = bytes(range(256)) * 500  # 128 KB of non-UTF-8 bytes
    await ingest_object(
        index=index,
        objects=objects,
        tenant=TENANT,
        att_id="a1",
        filename="blob.bin",
        content_type="application/octet-stream",
        data=payload,
    )
    async with _client(store) as client:
        listing = await client.get("/export/blobs", params={"tenant_id": TENANT})
        refs = [BlobRef.model_validate_json(x) for x in listing.text.splitlines() if x]
        assert [r.id for r in refs] == ["uploads/a1-blob.bin"]
        fetched = await client.get(
            "/export/blobs/uploads/a1-blob.bin", params={"tenant_id": TENANT}
        )
        assert fetched.content == payload

        objects.mem.clear()
        put = await client.put(
            "/import/blobs/uploads/a1-blob.bin",
            params={
                "tenant_id": TENANT,
                "sha256": _sha(payload),
                "size": len(payload),
                "content_type": "application/octet-stream",
            },
            content=payload,
        )
    assert put.json()["outcome"] == "created"
    stored = await objects.get_object(tenant=TENANT, key="uploads/a1-blob.bin")
    assert stored is not None
    assert stored.data == payload


async def test_a_corrupt_body_never_becomes_an_object(
    objects: MemObjectStore, store: StoragePortability
) -> None:
    async with _client(store) as client:
        response = await client.put(
            "/import/blobs/uploads/x.bin",
            params={"tenant_id": TENANT, "sha256": _sha(b"expected"), "size": len(b"corrupt")},
            content=b"corrupt",
        )
    assert response.status_code == 400
    assert objects.mem == {}


async def test_verification_runs_before_anything_is_published(
    objects: MemObjectStore, store: StoragePortability
) -> None:
    """The store consumes the whole (verified) stream before writing — so a mismatch aborts."""
    payload = b"a" * 4096
    with pytest.raises(ValueError, match="digest mismatch"):
        await store.put_blob(
            tenant_id=TENANT,
            blob_id="x.bin",
            sha256=_sha(b"different"),
            size=len(payload),
            content_type="application/octet-stream",
            chunks=verified_chunks(_chunks(payload), sha256=_sha(b"different"), size=len(payload)),
        )
    assert objects.mem == {}


async def test_a_missing_blob_is_a_404_over_the_route(store: StoragePortability) -> None:
    async with _client(store) as client:
        response = await client.get("/export/blobs/nothing/here", params={"tenant_id": TENANT})
    assert response.status_code == 404


async def _chunks_impl(data: bytes, size: int = 1024) -> AsyncIterator[bytes]:
    for start in range(0, len(data), size):
        yield data[start : start + size]


def _chunks(data: bytes, size: int = 1024) -> AsyncIterator[bytes]:
    return _chunks_impl(data, size)
