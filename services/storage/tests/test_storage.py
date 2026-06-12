"""Unit tests for storage MCP tools using an in-process SQLite index."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_storage.db import FileIndex
from epicurus_storage.scanner import scan
from epicurus_storage.service import build_module

TENANT = "test"


@pytest.fixture
async def tmp_index() -> FileIndex:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    idx = FileIndex(engine)
    await idx.init()
    return idx


@pytest.fixture
def sample_tree(tmp_path: Path) -> Path:
    """Creates:
    docs/
      readme.txt  (10 bytes)
      notes.md    (5 bytes)
    images/
      photo.jpg   (1024 bytes)
    """
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "readme.txt").write_text("0123456789")
    (tmp_path / "docs" / "notes.md").write_text("hello")
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "photo.jpg").write_bytes(b"\xff" * 1024)
    return tmp_path


async def test_scan_populates_index(tmp_index: FileIndex, sample_tree: Path) -> None:
    total = await scan(sample_tree, tmp_index, tenant=TENANT)
    assert total == 5  # docs/, docs/readme.txt, docs/notes.md, images/, images/photo.jpg


async def test_browse_root(tmp_index: FileIndex, sample_tree: Path) -> None:
    await scan(sample_tree, tmp_index, tenant=TENANT)
    entries = await tmp_index.browse(tenant=TENANT, path="")
    names = {e.name for e in entries}
    assert names == {"docs", "images"}


async def test_browse_subdir(tmp_index: FileIndex, sample_tree: Path) -> None:
    await scan(sample_tree, tmp_index, tenant=TENANT)
    entries = await tmp_index.browse(tenant=TENANT, path="docs")
    names = {e.name for e in entries}
    assert names == {"readme.txt", "notes.md"}


async def test_search_by_name(tmp_index: FileIndex, sample_tree: Path) -> None:
    await scan(sample_tree, tmp_index, tenant=TENANT)
    results = await tmp_index.search(tenant=TENANT, query="readme")
    assert len(results) == 1
    assert results[0].name == "readme.txt"


async def test_search_case_insensitive(tmp_index: FileIndex, sample_tree: Path) -> None:
    await scan(sample_tree, tmp_index, tenant=TENANT)
    results = await tmp_index.search(tenant=TENANT, query="README")
    assert len(results) == 1


async def test_scan_removes_stale_entries(tmp_index: FileIndex, sample_tree: Path) -> None:
    await scan(sample_tree, tmp_index, tenant=TENANT)
    (sample_tree / "docs" / "readme.txt").unlink()
    await scan(sample_tree, tmp_index, tenant=TENANT)
    results = await tmp_index.search(tenant=TENANT, query="readme")
    assert results == []


async def test_mcp_tool_browse(tmp_index: FileIndex, sample_tree: Path) -> None:
    await scan(sample_tree, tmp_index, tenant=TENANT)
    module = build_module(tmp_index, storage_root=str(sample_tree), tenant=TENANT)
    _content, structured = await module.mcp.call_tool("storage_browse", {"path": ""})
    assert isinstance(structured, dict)
    assert "result" in structured


async def test_mcp_tool_search(tmp_index: FileIndex, sample_tree: Path) -> None:
    await scan(sample_tree, tmp_index, tenant=TENANT)
    module = build_module(tmp_index, storage_root=str(sample_tree), tenant=TENANT)
    _content, structured = await module.mcp.call_tool("storage_search", {"query": "photo"})
    assert isinstance(structured, dict)


async def test_mcp_tool_search_empty_query(tmp_index: FileIndex, sample_tree: Path) -> None:
    await scan(sample_tree, tmp_index, tenant=TENANT)
    module = build_module(tmp_index, storage_root=str(sample_tree), tenant=TENANT)
    _content, structured = await module.mcp.call_tool("storage_search", {"query": "   "})
    assert structured == {"result": []}


async def test_mcp_tool_rescan(tmp_index: FileIndex, sample_tree: Path) -> None:
    module = build_module(tmp_index, storage_root=str(sample_tree), tenant=TENANT)
    _content, structured = await module.mcp.call_tool("storage_rescan", {})
    assert isinstance(structured, dict)
    # FastMCP may return dict results directly or wrapped under "result"
    payload: dict[str, object] = structured.get("result") or structured  # type: ignore[assignment]
    assert payload.get("total", 0) > 0


async def test_manifest_declares_tools_and_event(tmp_index: FileIndex) -> None:
    module = build_module(tmp_index, storage_root="/data", tenant=TENANT)
    manifest = await module.manifest()
    tool_names = {t.name for t in manifest.tools}
    assert {"storage_browse", "storage_search", "storage_rescan"} <= tool_names
    assert any(e.subject == "storage.scan.completed" for e in manifest.events_emitted)


async def test_tenant_isolation(tmp_index: FileIndex, sample_tree: Path) -> None:
    await scan(sample_tree, tmp_index, tenant="tenant-a")
    # tenant-b should see nothing
    entries = await tmp_index.browse(tenant="tenant-b", path="")
    assert entries == []
