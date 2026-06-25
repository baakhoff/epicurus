"""Unit tests for the chat upload sink (ADR-0025): ingest + object-backed download.

Runs against an in-process SQLite index and an in-memory object store, so no MinIO is
needed. The real MinIO round-trip for ``put_bytes``/``get_object`` is covered (under the
integration marker) in test_storage_objects.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_storage.db import FileIndex
from epicurus_storage.object_store import ObjectStore, StoredObject
from epicurus_storage.service import (
    UPLOADS_PREFIX,
    build_module,
    ingest_object,
    load_object_download,
    put_object,
)

TENANT = "test"


class _FakeObjectStore(ObjectStore):
    """In-memory substitute for the binary surface — no MinIO required."""

    def __init__(self) -> None:
        super().__init__(url="http://unused", access_key="x", secret_key="x")
        self._mem: dict[str, StoredObject] = {}

    async def put_bytes(
        self, *, tenant: str, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> None:
        self._mem[f"{tenant}\x00{key}"] = StoredObject(data=data, content_type=content_type)

    async def get_object(self, *, tenant: str, key: str) -> StoredObject | None:
        return self._mem.get(f"{tenant}\x00{key}")

    def drop(self, *, tenant: str, key: str) -> None:
        self._mem.pop(f"{tenant}\x00{key}", None)


@pytest.fixture
async def index() -> FileIndex:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    idx = FileIndex(engine)
    await idx.init()
    return idx


@pytest.fixture
def objects() -> _FakeObjectStore:
    return _FakeObjectStore()


# ── ingest_object ────────────────────────────────────────────────────────────


async def test_ingest_stores_bytes_and_makes_them_browsable(
    index: FileIndex, objects: _FakeObjectStore
) -> None:
    data = b"%PDF-1.4 fake pdf bytes"
    meta = await ingest_object(
        index=index,
        objects=objects,
        tenant=TENANT,
        att_id="abc123",
        filename="report.pdf",
        content_type="application/pdf",
        data=data,
    )
    assert meta == {"key": "uploads/abc123-report.pdf", "name": "report.pdf", "size": len(data)}

    # The bytes landed in the object store with their content type.
    stored = await objects.get_object(tenant=TENANT, key=str(meta["key"]))
    assert stored is not None
    assert stored.data == data
    assert stored.content_type == "application/pdf"

    # The Files browser shows an "uploads" folder at the root…
    root = await index.browse(tenant=TENANT, path="")
    uploads = [e for e in root if e.name == UPLOADS_PREFIX]
    assert len(uploads) == 1 and uploads[0].kind == "dir"

    # …and the file inside it, under its original display name.
    inside = await index.browse(tenant=TENANT, path=UPLOADS_PREFIX)
    assert len(inside) == 1
    entry = inside[0]
    assert entry.name == "report.pdf"
    assert entry.kind == "file"
    assert entry.source == "object"
    assert entry.path == "uploads/abc123-report.pdf"
    assert entry.size == len(data)


async def test_ingest_sanitises_the_filename(index: FileIndex, objects: _FakeObjectStore) -> None:
    meta = await ingest_object(
        index=index,
        objects=objects,
        tenant=TENANT,
        att_id="t1",
        filename="../../etc/passwd",
        content_type="text/plain",
        data=b"x",
    )
    # The key has no traversal segments and the display name is the basename.
    assert meta["key"] == "uploads/t1-passwd"
    assert meta["name"] == "passwd"


async def test_ingest_without_att_id_generates_a_unique_token(
    index: FileIndex, objects: _FakeObjectStore
) -> None:
    first = await ingest_object(
        index=index,
        objects=objects,
        tenant=TENANT,
        att_id="",
        filename="a.txt",
        content_type="text/plain",
        data=b"a",
    )
    second = await ingest_object(
        index=index,
        objects=objects,
        tenant=TENANT,
        att_id="",
        filename="a.txt",
        content_type="text/plain",
        data=b"b",
    )
    assert first["key"] != second["key"]
    assert str(first["key"]).startswith("uploads/")
    # Both remain browsable (distinct object keys, same display name).
    inside = await index.browse(tenant=TENANT, path=UPLOADS_PREFIX)
    assert len(inside) == 2


async def test_uploads_are_findable_by_search(index: FileIndex, objects: _FakeObjectStore) -> None:
    await ingest_object(
        index=index,
        objects=objects,
        tenant=TENANT,
        att_id="z9",
        filename="quarterly-report.pdf",
        content_type="application/pdf",
        data=b"x",
    )
    hits = await index.search(tenant=TENANT, query="quarterly")
    assert any(h.name == "quarterly-report.pdf" and h.source == "object" for h in hits)


# ── load_object_download ─────────────────────────────────────────────────────


async def test_download_resolves_an_uploaded_object(
    index: FileIndex, objects: _FakeObjectStore
) -> None:
    data = b"the bytes"
    meta = await ingest_object(
        index=index,
        objects=objects,
        tenant=TENANT,
        att_id="d1",
        filename="memo.txt",
        content_type="text/plain",
        data=data,
    )
    dl = await load_object_download(
        index=index, objects=objects, tenant=TENANT, path=str(meta["key"])
    )
    assert dl is not None
    assert dl.name == "memo.txt"
    assert dl.data == data
    assert dl.content_type == "text/plain"


async def test_download_returns_none_for_a_filesystem_entry(
    index: FileIndex, objects: _FakeObjectStore
) -> None:
    # A scanned (source="fs") row must NOT resolve as an object download.
    await index.upsert_batch(
        tenant=TENANT,
        entries=[
            {
                "path": "docs/readme.txt",
                "name": "readme.txt",
                "size": 3,
                "mtime": 0.0,
                "kind": "file",
            }
        ],
    )
    dl = await load_object_download(
        index=index, objects=objects, tenant=TENANT, path="docs/readme.txt"
    )
    assert dl is None


async def test_download_returns_none_for_unknown_path(
    index: FileIndex, objects: _FakeObjectStore
) -> None:
    dl = await load_object_download(
        index=index, objects=objects, tenant=TENANT, path="uploads/missing"
    )
    assert dl is None


async def test_download_returns_none_when_bytes_are_gone(
    index: FileIndex, objects: _FakeObjectStore
) -> None:
    # Catalogued in the index but the object is missing from the store → no download.
    meta = await ingest_object(
        index=index,
        objects=objects,
        tenant=TENANT,
        att_id="g1",
        filename="gone.bin",
        content_type="application/octet-stream",
        data=b"x",
    )
    objects.drop(tenant=TENANT, key=str(meta["key"]))
    dl = await load_object_download(
        index=index, objects=objects, tenant=TENANT, path=str(meta["key"])
    )
    assert dl is None


# ── index: source column + rescan survival + migration ───────────────────────


async def test_purge_stale_keeps_object_uploads(
    index: FileIndex, objects: _FakeObjectStore
) -> None:
    # One scanned file and one uploaded object…
    await index.upsert_batch(
        tenant=TENANT,
        entries=[{"path": "old.txt", "name": "old.txt", "size": 1, "mtime": 0.0, "kind": "file"}],
    )
    await ingest_object(
        index=index,
        objects=objects,
        tenant=TENANT,
        att_id="k1",
        filename="keep.txt",
        content_type="text/plain",
        data=b"x",
    )
    # …a rescan that sees neither path purges only the filesystem row.
    deleted = await index.purge_stale(tenant=TENANT, seen_paths=set())
    assert deleted == 1
    paths = {e.path for e in await index.browse(tenant=TENANT, path=UPLOADS_PREFIX)}
    assert "uploads/k1-keep.txt" in paths
    root_names = {e.name for e in await index.browse(tenant=TENANT, path="")}
    assert "old.txt" not in root_names
    assert UPLOADS_PREFIX in root_names


async def test_upsert_batch_defaults_source_to_fs(index: FileIndex) -> None:
    await index.upsert_batch(
        tenant=TENANT,
        entries=[{"path": "f.txt", "name": "f.txt", "size": 1, "mtime": 0.0, "kind": "file"}],
    )
    entry = await index.get(tenant=TENANT, path="f.txt")
    assert entry is not None and entry.source == "fs"


async def test_init_adds_source_column_to_a_legacy_table() -> None:
    """A pre-source deployment gains the column at init, backfilled to 'fs'."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE storage_files ("
            "id INTEGER PRIMARY KEY, tenant VARCHAR(63), path VARCHAR(4096), "
            "name VARCHAR(255), size BIGINT, mtime FLOAT, kind VARCHAR(8), updated_at DATETIME)"
        )
        await conn.exec_driver_sql(
            "INSERT INTO storage_files (tenant, path, name, size, mtime, kind, updated_at) "
            "VALUES ('test', 'docs/readme.txt', 'readme.txt', 10, 0, 'file', '2026-01-01 00:00:00')"
        )
    idx = FileIndex(engine)
    await idx.init()  # idempotent: adds the missing 'source' column
    await idx.init()  # second call is a no-op (column already present)
    entry = await idx.get(tenant="test", path="docs/readme.txt")
    assert entry is not None
    assert entry.source == "fs"


# ── put_object: an agent-written file appears in the Files UI (#347) ───────────


async def test_put_object_indexes_so_the_browser_lists_it(
    index: FileIndex, objects: _FakeObjectStore
) -> None:
    # The bug: storage_object_put wrote bytes to MinIO but created no index row, so the Files
    # browser — which lists the index, not the bucket — never showed the file. Now it does.
    result = await put_object(
        index=index, objects=objects, tenant=TENANT, key="report.md", content="hello"
    )
    assert result == {"status": "ok", "key": "report.md"}

    root = await index.browse(tenant=TENANT, path="")
    entry = next(e for e in root if e.name == "report.md")
    assert entry.kind == "file"
    assert entry.source == "object"
    assert entry.path == "report.md"
    assert entry.size == len(b"hello")

    # …and it resolves as an object download (bytes served from the store, not the disk tree).
    dl = await load_object_download(index=index, objects=objects, tenant=TENANT, path="report.md")
    assert dl is not None and dl.data == b"hello"


async def test_put_object_nested_key_creates_navigable_folders(
    index: FileIndex, objects: _FakeObjectStore
) -> None:
    await put_object(
        index=index, objects=objects, tenant=TENANT, key="reports/2026/q2.md", content="x"
    )
    # Each ancestor segment is a navigable directory row, down to the file itself.
    assert "reports" in {e.name for e in await index.browse(tenant=TENANT, path="")}
    assert "2026" in {e.name for e in await index.browse(tenant=TENANT, path="reports")}
    leaf = await index.browse(tenant=TENANT, path="reports/2026")
    assert [(e.name, e.kind, e.source) for e in leaf] == [("q2.md", "file", "object")]


async def test_put_object_normalizes_key_and_strips_traversal(
    index: FileIndex, objects: _FakeObjectStore
) -> None:
    # Redundant separators / "." collapse and ".." is dropped — no traversal in the index
    # path, and the one normalised key addresses both the store and the index.
    result = await put_object(
        index=index, objects=objects, tenant=TENANT, key="../docs//./guide.md", content="g"
    )
    assert result["key"] == "docs/guide.md"
    dl = await load_object_download(
        index=index, objects=objects, tenant=TENANT, path="docs/guide.md"
    )
    assert dl is not None and dl.data == b"g"


async def test_put_object_is_findable_by_search(
    index: FileIndex, objects: _FakeObjectStore
) -> None:
    await put_object(
        index=index, objects=objects, tenant=TENANT, key="quarterly-summary.md", content="x"
    )
    hits = await index.search(tenant=TENANT, query="quarterly")
    assert any(h.name == "quarterly-summary.md" and h.source == "object" for h in hits)


async def test_put_object_survives_a_rescan(index: FileIndex, objects: _FakeObjectStore) -> None:
    # Object rows persist across a directory rescan (purge_stale removes only fs rows).
    await put_object(index=index, objects=objects, tenant=TENANT, key="keep.md", content="k")
    deleted = await index.purge_stale(tenant=TENANT, seen_paths=set())
    assert deleted == 0
    assert "keep.md" in {e.name for e in await index.browse(tenant=TENANT, path="")}


async def test_agent_reads_back_an_object_it_saved(
    index: FileIndex, objects: _FakeObjectStore, tmp_path: Path
) -> None:
    # storage_read serves source="object" entries from the store, so a file the agent saved
    # via storage_object_put reads back through the same tool that now lists it.
    module = build_module(index, objects, storage_root=str(tmp_path), tenant=TENANT)
    await module.mcp.call_tool(
        "storage_object_put", {"key": "memo.md", "content": "agent wrote it"}
    )
    _content, structured = await module.mcp.call_tool("storage_read", {"path": "memo.md"})
    payload = structured.get("result") or structured
    assert payload == "agent wrote it"
