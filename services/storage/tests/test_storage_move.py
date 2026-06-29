"""Unit tests for Files-browser move/rename (#381 / #391): object move + index re-path.

Runs against an in-process SQLite index and an in-memory object store, so no MinIO is needed.
Covers the writable move path (rename, move into a folder, whole-folder re-key) and the
read-only guard — a scanned (``source="fs"``) entry cannot be moved.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_storage.db import FileIndex
from epicurus_storage.object_store import ObjectStore, StoredObject
from epicurus_storage.service import move_item, put_object

TENANT = "test"


class _FakeObjectStore(ObjectStore):
    """In-memory object store covering the four ops move uses (put/get/copy/delete)."""

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

    async def copy(self, *, tenant: str, src_key: str, dst_key: str) -> None:
        self._mem[self._k(tenant, dst_key)] = self._mem[self._k(tenant, src_key)]

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


async def test_rename_object_file_in_place(index: FileIndex, objects: _FakeObjectStore) -> None:
    await _put(index, objects, "notes/draft.md", "hi")
    result = await move_item(
        index=index,
        objects=objects,
        tenant=TENANT,
        from_path="notes/draft.md",
        to_path="notes/final.md",
    )
    assert result == {"path": "notes/final.md"}
    # Index re-pathed (display name follows).
    assert await index.get(tenant=TENANT, path="notes/draft.md") is None
    moved = await index.get(tenant=TENANT, path="notes/final.md")
    assert moved is not None and moved.source == "object" and moved.name == "final.md"
    # Bytes followed in the store.
    assert await objects.get(tenant=TENANT, key="notes/draft.md") is None
    assert await objects.get(tenant=TENANT, key="notes/final.md") == "hi"


async def test_move_file_into_new_folder_creates_it(
    index: FileIndex, objects: _FakeObjectStore
) -> None:
    await _put(index, objects, "a.md", "A")
    await move_item(
        index=index, objects=objects, tenant=TENANT, from_path="a.md", to_path="archive/a.md"
    )
    assert await objects.get(tenant=TENANT, key="archive/a.md") == "A"
    # The new "archive" folder is navigable from the root, and holds the moved file.
    assert "archive" in {e.name for e in await index.browse(tenant=TENANT, path="")}
    leaf = await index.browse(tenant=TENANT, path="archive")
    assert [(e.name, e.source) for e in leaf] == [("a.md", "object")]


async def test_move_whole_folder_rekeys_subtree(
    index: FileIndex, objects: _FakeObjectStore
) -> None:
    await _put(index, objects, "proj/a.md", "A")
    await _put(index, objects, "proj/sub/b.md", "B")
    result = await move_item(
        index=index, objects=objects, tenant=TENANT, from_path="proj", to_path="done"
    )
    assert result == {"path": "done"}
    assert await objects.get(tenant=TENANT, key="done/a.md") == "A"
    assert await objects.get(tenant=TENANT, key="done/sub/b.md") == "B"
    assert await index.get(tenant=TENANT, path="proj") is None
    assert await index.get(tenant=TENANT, path="done/sub/b.md") is not None
    # Old object keys are gone.
    assert await objects.get(tenant=TENANT, key="proj/a.md") is None


async def test_same_path_is_a_noop(index: FileIndex, objects: _FakeObjectStore) -> None:
    await _put(index, objects, "a.md", "A")
    assert await move_item(
        index=index, objects=objects, tenant=TENANT, from_path="a.md", to_path="a.md"
    ) == {"path": "a.md"}
    assert await objects.get(tenant=TENANT, key="a.md") == "A"


# ── guards ───────────────────────────────────────────────────────────────────


async def test_scanned_file_is_read_only(index: FileIndex, objects: _FakeObjectStore) -> None:
    await index.upsert_batch(
        tenant=TENANT,
        entries=[{"path": "docs/r.txt", "name": "r.txt", "size": 3, "mtime": 0.0, "kind": "file"}],
    )
    with pytest.raises(HTTPException) as ei:
        await move_item(
            index=index,
            objects=objects,
            tenant=TENANT,
            from_path="docs/r.txt",
            to_path="docs/moved.txt",
        )
    assert ei.value.status_code == 400
    assert await index.get(tenant=TENANT, path="docs/r.txt") is not None  # untouched


async def test_folder_with_a_scanned_child_is_read_only(
    index: FileIndex, objects: _FakeObjectStore
) -> None:
    # A mixed subtree (one object file, one scanned file) cannot be moved.
    await _put(index, objects, "mix/up.md", "u")
    await index.upsert_batch(
        tenant=TENANT,
        entries=[
            {"path": "mix/scan.txt", "name": "scan.txt", "size": 1, "mtime": 0.0, "kind": "file"}
        ],
    )
    with pytest.raises(HTTPException) as ei:
        await move_item(
            index=index, objects=objects, tenant=TENANT, from_path="mix", to_path="moved"
        )
    assert ei.value.status_code == 400


async def test_missing_source_is_404(index: FileIndex, objects: _FakeObjectStore) -> None:
    with pytest.raises(HTTPException) as ei:
        await move_item(
            index=index, objects=objects, tenant=TENANT, from_path="ghost.md", to_path="x.md"
        )
    assert ei.value.status_code == 404


async def test_destination_taken_is_409(index: FileIndex, objects: _FakeObjectStore) -> None:
    await _put(index, objects, "a.md", "A")
    await _put(index, objects, "b.md", "B")
    with pytest.raises(HTTPException) as ei:
        await move_item(
            index=index, objects=objects, tenant=TENANT, from_path="a.md", to_path="b.md"
        )
    assert ei.value.status_code == 409


async def test_move_into_itself_is_400(index: FileIndex, objects: _FakeObjectStore) -> None:
    await _put(index, objects, "proj/a.md", "A")
    with pytest.raises(HTTPException) as ei:
        await move_item(
            index=index, objects=objects, tenant=TENANT, from_path="proj", to_path="proj/child"
        )
    assert ei.value.status_code == 400


async def test_root_move_is_400(index: FileIndex, objects: _FakeObjectStore) -> None:
    with pytest.raises(HTTPException) as ei:
        await move_item(index=index, objects=objects, tenant=TENANT, from_path="", to_path="x")
    assert ei.value.status_code == 400
