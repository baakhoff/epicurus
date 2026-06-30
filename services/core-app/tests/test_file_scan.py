"""Unit tests for the core file-space scanner (ADR-0063) over a real LocalFileStore."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core.files import LocalFileStore
from epicurus_core_app.file_index import FileIndex
from epicurus_core_app.file_scan import scan

TENANT = "local"


async def _fresh_index() -> tuple[FileIndex, AsyncEngine]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    index = FileIndex(engine)
    await index.init()
    return index, engine


def _tree(root: Path) -> None:
    tenant_root = root / TENANT
    (tenant_root / "knowledge").mkdir(parents=True)
    (tenant_root / "knowledge" / "readme.md").write_text("hello", encoding="utf-8")
    (tenant_root / "notes").mkdir()
    (tenant_root / "notes" / "todo.md").write_text("x", encoding="utf-8")
    (tenant_root / "top.txt").write_text("abc", encoding="utf-8")


async def test_scan_indexes_the_whole_tree(tmp_path: Path) -> None:
    _tree(tmp_path)
    store = LocalFileStore(tmp_path)
    index, _ = await _fresh_index()

    total = await scan(store, index, tenant=TENANT)
    # 2 dirs + 3 files (the tenant root itself is not indexed).
    assert total == 5
    assert await index.count(tenant=TENANT) == {"files": 3, "dirs": 2}
    assert {e.path for e in await index.browse(tenant=TENANT, path="")} == {
        "knowledge",
        "notes",
        "top.txt",
    }
    assert {e.path for e in await index.browse(tenant=TENANT, path="knowledge")} == {
        "knowledge/readme.md"
    }
    entry = await index.get(tenant=TENANT, path="top.txt")
    assert entry is not None and entry.size == 3


async def test_rescan_picks_up_adds_and_purges_deletes(tmp_path: Path) -> None:
    _tree(tmp_path)
    store = LocalFileStore(tmp_path)
    index, _ = await _fresh_index()
    await scan(store, index, tenant=TENANT)

    # Add a file, remove another, then rescan: the index converges to disk.
    (tmp_path / TENANT / "knowledge" / "new.md").write_text("n", encoding="utf-8")
    (tmp_path / TENANT / "top.txt").unlink()
    await scan(store, index, tenant=TENANT)

    paths = {e.path for e in await index.search(tenant=TENANT, query="")}
    # search("") matches everything (empty substring); confirms both convergence directions.
    assert "knowledge/new.md" in paths
    assert "top.txt" not in paths


async def test_scan_of_empty_root_is_clean(tmp_path: Path) -> None:
    (tmp_path / TENANT).mkdir(parents=True)
    store = LocalFileStore(tmp_path)
    index, _ = await _fresh_index()
    assert await scan(store, index, tenant=TENANT) == 0
    assert await index.count(tenant=TENANT) == {"files": 0, "dirs": 0}
