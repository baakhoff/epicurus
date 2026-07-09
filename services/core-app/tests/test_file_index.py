"""Unit tests for the core-owned file index (ADR-0063), tenant-scoped over the file space.

In-memory SQLite with StaticPool (the test_module_prefs.py pattern) — pure DB I/O, no FileStore.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core_app.file_index import FileIndex

TENANT = "local"


async def _fresh() -> tuple[FileIndex, AsyncEngine]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    index = FileIndex(engine)
    await index.init()
    return index, engine


def _row(path: str, name: str, kind: str, size: int = 0) -> dict[str, object]:
    return {"path": path, "name": name, "size": size, "mtime": 1.0, "kind": kind}


async def _seed(index: FileIndex) -> None:
    await index.upsert_batch(
        tenant=TENANT,
        entries=[
            _row("knowledge", "knowledge", "dir"),
            _row("knowledge/readme.md", "readme.md", "file", 12),
            _row("notes", "notes", "dir"),
            _row("notes/todo.md", "todo.md", "file", 5),
            _row("top.txt", "top.txt", "file", 3),
        ],
    )


async def test_browse_returns_direct_children_dirs_first() -> None:
    index, _ = await _fresh()
    await _seed(index)
    root = await index.browse(tenant=TENANT, path="")
    assert [e.name for e in root] == ["knowledge", "notes", "top.txt"]
    assert [e.kind for e in root] == ["dir", "dir", "file"]
    # A nested directory returns only its own direct children.
    kids = await index.browse(tenant=TENANT, path="knowledge")
    assert [e.path for e in kids] == ["knowledge/readme.md"]


async def test_get_and_count() -> None:
    index, _ = await _fresh()
    await _seed(index)
    entry = await index.get(tenant=TENANT, path="top.txt")
    assert entry is not None and entry.kind == "file" and entry.size == 3
    assert await index.get(tenant=TENANT, path="nope") is None
    assert await index.count(tenant=TENANT) == {"files": 3, "dirs": 2}


async def test_search_matches_name_and_path_case_insensitively() -> None:
    index, _ = await _fresh()
    await _seed(index)
    by_name = await index.search(tenant=TENANT, query="README")
    assert {e.path for e in by_name} == {"knowledge/readme.md"}
    by_path = await index.search(tenant=TENANT, query="notes/")
    assert {e.path for e in by_path} == {"notes/todo.md"}


async def test_upsert_is_idempotent_by_path() -> None:
    index, _ = await _fresh()
    await _seed(index)
    await index.upsert_batch(tenant=TENANT, entries=[_row("top.txt", "top.txt", "file", 99)])
    entry = await index.get(tenant=TENANT, path="top.txt")
    assert entry is not None and entry.size == 99
    assert await index.count(tenant=TENANT) == {"files": 3, "dirs": 2}


async def test_purge_stale_removes_unseen_rows() -> None:
    index, _ = await _fresh()
    await _seed(index)
    deleted = await index.purge_stale(
        tenant=TENANT, seen_paths={"knowledge", "knowledge/readme.md"}
    )
    assert deleted == 3
    assert {e.path for e in await index.browse(tenant=TENANT, path="")} == {"knowledge"}


async def test_tenant_isolation() -> None:
    index, _ = await _fresh()
    await _seed(index)
    await index.upsert_batch(tenant="other", entries=[_row("secret.txt", "secret.txt", "file", 1)])
    assert {e.path for e in await index.browse(tenant=TENANT, path="")} == {
        "knowledge",
        "notes",
        "top.txt",
    }
    assert {e.path for e in await index.browse(tenant="other", path="")} == {"secret.txt"}


async def test_remove_subtree_does_not_sweep_a_wildcard_sibling() -> None:
    # A folder name may contain ``_`` — a SQL LIKE wildcard matching any single character.
    # De-indexing "data_2024" must not also drop a sibling "data-2024" that differs only where
    # the wildcard sits. Unescaped, ``LIKE 'data_2024/%'`` matches "data-2024/report.md" and
    # would remove it from search/listing until the #390 watcher re-indexed it.
    index, _ = await _fresh()
    await index.upsert_batch(
        tenant=TENANT,
        entries=[
            _row("data_2024", "data_2024", "dir"),
            _row("data_2024/report.md", "report.md", "file", 4),
            _row("data-2024", "data-2024", "dir"),
            _row("data-2024/report.md", "report.md", "file", 4),
        ],
    )
    removed = await index.remove_subtree(tenant=TENANT, path="data_2024")
    assert removed == 2  # the data_2024 dir row + its one file — never the sibling's rows
    # The named folder is de-indexed…
    assert await index.get(tenant=TENANT, path="data_2024/report.md") is None
    # …and the sibling survives. This is the regression guard: unescaped LIKE would drop it.
    assert await index.get(tenant=TENANT, path="data-2024/report.md") is not None
    assert await index.get(tenant=TENANT, path="data-2024") is not None
