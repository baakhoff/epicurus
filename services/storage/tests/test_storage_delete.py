"""Unit tests for Files-browser object delete (#564): object removal + index de-key.

Runs against an in-process SQLite index and an in-memory object store, so no MinIO is needed.
Covers the writable delete path (single file, whole-folder subtree), the idempotent miss, and
the read-only guard — a scanned (``source="fs"``) entry cannot be deleted through this door.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_storage.db import FileIndex
from epicurus_storage.object_store import ObjectStore, StoredObject
from epicurus_storage.service import delete_item, put_object

TENANT = "test"


class _FakeObjectStore(ObjectStore):
    """In-memory object store covering the ops delete uses (put/get/delete)."""

    def __init__(self) -> None:
        super().__init__(url="http://unused", access_key="x", secret_key="x")
        self._mem: dict[str, StoredObject] = {}

    def _k(self, tenant: str, key: str) -> str:
        return f"{tenant}\x00{key}"

    async def put_bytes(
        self, *, tenant: str, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> None:
        self._mem[self._k(tenant, key)] = StoredObject(data=data, content_type=content_type)

    async def get_object(self, *, tenant: str, key: str) -> StoredObject | None:
        return self._mem.get(self._k(tenant, key))

    async def delete(self, *, tenant: str, key: str) -> None:
        self._mem.pop(self._k(tenant, key), None)


@pytest.fixture
async def index() -> AsyncIterator[FileIndex]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    idx = FileIndex(engine)
    await idx.init()
    yield idx
    await engine.dispose()


@pytest.fixture
def objects() -> _FakeObjectStore:
    return _FakeObjectStore()


async def _put(index: FileIndex, objects: _FakeObjectStore, key: str, content: str) -> None:
    await put_object(index=index, objects=objects, tenant=TENANT, key=key, content=content)


# ── happy paths ────────────────────────────────────────────────────────────────


async def test_delete_object_file(index: FileIndex, objects: _FakeObjectStore) -> None:
    await _put(index, objects, "notes/draft.md", "hi")
    result = await delete_item(index=index, objects=objects, tenant=TENANT, path="notes/draft.md")
    assert result == {"deleted": True}
    # Index row gone; bytes gone from the store.
    assert await index.get(tenant=TENANT, path="notes/draft.md") is None
    assert await objects.get(tenant=TENANT, key="notes/draft.md") is None
    # The parent "notes" folder row survives — only the named entry (its subtree) is removed.
    assert "notes" in {e.name for e in await index.browse(tenant=TENANT, path="")}


async def test_delete_whole_folder_removes_subtree(
    index: FileIndex, objects: _FakeObjectStore
) -> None:
    await _put(index, objects, "proj/a.md", "A")
    await _put(index, objects, "proj/sub/b.md", "B")
    result = await delete_item(index=index, objects=objects, tenant=TENANT, path="proj")
    assert result == {"deleted": True}
    # Every row under proj/ is gone, plus the proj row itself.
    assert await index.get(tenant=TENANT, path="proj") is None
    assert await index.get(tenant=TENANT, path="proj/sub/b.md") is None
    assert await index.browse(tenant=TENANT, path="") == []
    # Every object's bytes followed.
    assert await objects.get(tenant=TENANT, key="proj/a.md") is None
    assert await objects.get(tenant=TENANT, key="proj/sub/b.md") is None


async def test_delete_missing_is_deleted_false(index: FileIndex, objects: _FakeObjectStore) -> None:
    # Idempotent: nothing at the path is a clean False, not an error (matches the FileStore seam).
    assert await delete_item(index=index, objects=objects, tenant=TENANT, path="ghost.md") == {
        "deleted": False
    }


# ── guards ───────────────────────────────────────────────────────────────────


async def test_scanned_file_is_read_only(index: FileIndex, objects: _FakeObjectStore) -> None:
    await index.upsert_batch(
        tenant=TENANT,
        entries=[{"path": "docs/r.txt", "name": "r.txt", "size": 3, "mtime": 0.0, "kind": "file"}],
    )
    with pytest.raises(HTTPException) as ei:
        await delete_item(index=index, objects=objects, tenant=TENANT, path="docs/r.txt")
    assert ei.value.status_code == 400
    assert await index.get(tenant=TENANT, path="docs/r.txt") is not None  # untouched


async def test_folder_with_a_scanned_child_is_read_only(
    index: FileIndex, objects: _FakeObjectStore
) -> None:
    # A mixed subtree (one object file, one scanned file) cannot be deleted through this door.
    await _put(index, objects, "mix/up.md", "u")
    await index.upsert_batch(
        tenant=TENANT,
        entries=[
            {"path": "mix/scan.txt", "name": "scan.txt", "size": 1, "mtime": 0.0, "kind": "file"}
        ],
    )
    with pytest.raises(HTTPException) as ei:
        await delete_item(index=index, objects=objects, tenant=TENANT, path="mix")
    assert ei.value.status_code == 400
    # Nothing was removed — the whole subtree survives.
    assert await index.get(tenant=TENANT, path="mix/up.md") is not None
    assert await objects.get(tenant=TENANT, key="mix/up.md") == "u"


async def test_root_delete_is_400(index: FileIndex, objects: _FakeObjectStore) -> None:
    with pytest.raises(HTTPException) as ei:
        await delete_item(index=index, objects=objects, tenant=TENANT, path="")
    assert ei.value.status_code == 400


async def test_delete_subtree_index_only_helper(
    index: FileIndex, objects: _FakeObjectStore
) -> None:
    # The index half in isolation: delete_subtree drops the root + descendants and reports the
    # count; the empty-path root is a no-op.
    await _put(index, objects, "d/a.md", "A")
    await _put(index, objects, "d/b.md", "B")
    assert await index.delete_subtree(tenant=TENANT, path="") == 0
    removed = await index.delete_subtree(tenant=TENANT, path="d")
    assert removed == 3  # the "d" dir row + two files
    assert await index.browse(tenant=TENANT, path="") == []
