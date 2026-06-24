"""Unit tests for storage MCP tools using an in-process SQLite index."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_storage.db import FileIndex
from epicurus_storage.object_store import ObjectStore
from epicurus_storage.scanner import scan
from epicurus_storage.service import STORAGE_PAGE_ID, build_module, build_page_data

TENANT = "test"


class _FakeObjectStore(ObjectStore):
    """In-memory substitute for unit tests — no MinIO required.

    Calls ``super().__init__`` with unused credentials (aioboto3.Session is
    lightweight); overrides ``put``/``get`` so no network calls are made.
    """

    def __init__(self) -> None:
        super().__init__(url="http://unused", access_key="x", secret_key="x")
        self._mem: dict[str, str] = {}

    async def put(self, *, tenant: str, key: str, content: str) -> None:
        self._mem[f"{tenant}\x00{key}"] = content

    async def get(self, *, tenant: str, key: str) -> str | None:
        return self._mem.get(f"{tenant}\x00{key}")


@pytest.fixture
async def tmp_index() -> FileIndex:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    idx = FileIndex(engine)
    await idx.init()
    return idx


@pytest.fixture
def fake_objects() -> _FakeObjectStore:
    return _FakeObjectStore()


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


# ── File-index helpers ───────────────────────────────────────────────────────


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


async def test_db_count(tmp_index: FileIndex, sample_tree: Path) -> None:
    await scan(sample_tree, tmp_index, tenant=TENANT)
    counts = await tmp_index.count(tenant=TENANT)
    assert counts["files"] == 3  # readme.txt, notes.md, photo.jpg
    assert counts["dirs"] == 2  # docs/, images/


# ── MCP tool: storage_list ───────────────────────────────────────────────────


async def test_mcp_tool_list(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore, sample_tree: Path
) -> None:
    await scan(sample_tree, tmp_index, tenant=TENANT)
    module = build_module(tmp_index, fake_objects, storage_root=str(sample_tree), tenant=TENANT)
    _content, structured = await module.mcp.call_tool("storage_list", {"path": ""})
    assert isinstance(structured, dict)
    assert "result" in structured


async def test_mcp_tool_list_subdir(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore, sample_tree: Path
) -> None:
    await scan(sample_tree, tmp_index, tenant=TENANT)
    module = build_module(tmp_index, fake_objects, storage_root=str(sample_tree), tenant=TENANT)
    _content, structured = await module.mcp.call_tool("storage_list", {"path": "docs"})
    assert isinstance(structured, dict)


# ── MCP tool: storage_search ─────────────────────────────────────────────────


async def test_mcp_tool_search(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore, sample_tree: Path
) -> None:
    await scan(sample_tree, tmp_index, tenant=TENANT)
    module = build_module(tmp_index, fake_objects, storage_root=str(sample_tree), tenant=TENANT)
    _content, structured = await module.mcp.call_tool("storage_search", {"query": "photo"})
    assert isinstance(structured, dict)


async def test_mcp_tool_search_empty_query(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore, sample_tree: Path
) -> None:
    await scan(sample_tree, tmp_index, tenant=TENANT)
    module = build_module(tmp_index, fake_objects, storage_root=str(sample_tree), tenant=TENANT)
    _content, structured = await module.mcp.call_tool("storage_search", {"query": "   "})
    assert structured == {"result": []}


# ── MCP tool: storage_read ───────────────────────────────────────────────────


async def test_mcp_tool_read_text(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore, sample_tree: Path
) -> None:
    module = build_module(tmp_index, fake_objects, storage_root=str(sample_tree), tenant=TENANT)
    _content, structured = await module.mcp.call_tool("storage_read", {"path": "docs/readme.txt"})
    assert isinstance(structured, dict)
    payload = structured.get("result") or structured
    assert payload == "0123456789"


async def test_mcp_tool_read_missing(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore, sample_tree: Path
) -> None:
    module = build_module(tmp_index, fake_objects, storage_root=str(sample_tree), tenant=TENANT)
    _content, structured = await module.mcp.call_tool("storage_read", {"path": "docs/nope.txt"})
    payload = structured.get("result") or structured
    assert str(payload).startswith("Error:")


async def test_mcp_tool_read_binary(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore, sample_tree: Path
) -> None:
    module = build_module(tmp_index, fake_objects, storage_root=str(sample_tree), tenant=TENANT)
    _content, structured = await module.mcp.call_tool("storage_read", {"path": "images/photo.jpg"})
    payload = structured.get("result") or structured
    assert str(payload).startswith("Error:")


async def test_mcp_tool_read_too_large(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore, tmp_path: Path
) -> None:
    big = tmp_path / "big.txt"
    big.write_bytes(b"x" * (256 * 1024 + 1))
    module = build_module(tmp_index, fake_objects, storage_root=str(tmp_path), tenant=TENANT)
    _content, structured = await module.mcp.call_tool("storage_read", {"path": "big.txt"})
    payload = structured.get("result") or structured
    assert str(payload).startswith("Error:")


async def test_mcp_tool_read_traversal(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore, sample_tree: Path
) -> None:
    module = build_module(tmp_index, fake_objects, storage_root=str(sample_tree), tenant=TENANT)
    _content, structured = await module.mcp.call_tool("storage_read", {"path": "../../etc/passwd"})
    payload = structured.get("result") or structured
    assert str(payload).startswith("Error:")


async def test_mcp_tool_read_directory(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore, sample_tree: Path
) -> None:
    module = build_module(tmp_index, fake_objects, storage_root=str(sample_tree), tenant=TENANT)
    _content, structured = await module.mcp.call_tool("storage_read", {"path": "docs"})
    payload = structured.get("result") or structured
    assert str(payload).startswith("Error:")


# ── MCP tool: storage_status ─────────────────────────────────────────────────


async def test_mcp_tool_status(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore, sample_tree: Path
) -> None:
    await scan(sample_tree, tmp_index, tenant=TENANT)
    module = build_module(tmp_index, fake_objects, storage_root=str(sample_tree), tenant=TENANT)
    _content, structured = await module.mcp.call_tool("storage_status", {})
    assert isinstance(structured, dict)
    payload: dict[str, object] = structured.get("result") or structured  # type: ignore[assignment]
    assert "root" in payload
    assert payload.get("files", -1) == 3
    assert payload.get("dirs", -1) == 2


# ── MCP tool: storage_rescan ─────────────────────────────────────────────────


async def test_mcp_tool_rescan(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore, sample_tree: Path
) -> None:
    module = build_module(tmp_index, fake_objects, storage_root=str(sample_tree), tenant=TENANT)
    _content, structured = await module.mcp.call_tool("storage_rescan", {})
    assert isinstance(structured, dict)
    payload: dict[str, object] = structured.get("result") or structured  # type: ignore[assignment]
    assert payload.get("total", 0) > 0


# ── MCP tools: storage_object_put / storage_object_get ───────────────────────


async def test_mcp_tool_object_put_get(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore, tmp_path: Path
) -> None:
    module = build_module(tmp_index, fake_objects, storage_root=str(tmp_path), tenant=TENANT)

    _c, put_result = await module.mcp.call_tool(
        "storage_object_put", {"key": "report.txt", "content": "hello world"}
    )
    put_payload: dict[str, object] = put_result.get("result") or put_result  # type: ignore[assignment]
    assert put_payload.get("status") == "ok"
    assert put_payload.get("key") == "report.txt"

    _c, get_result = await module.mcp.call_tool("storage_object_get", {"key": "report.txt"})
    get_payload: dict[str, object] = get_result.get("result") or get_result  # type: ignore[assignment]
    assert get_payload.get("content") == "hello world"


async def test_mcp_tool_object_get_missing(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore, tmp_path: Path
) -> None:
    module = build_module(tmp_index, fake_objects, storage_root=str(tmp_path), tenant=TENANT)
    _c, get_result = await module.mcp.call_tool("storage_object_get", {"key": "does-not-exist.txt"})
    get_payload: dict[str, object] = get_result.get("result") or get_result  # type: ignore[assignment]
    assert get_payload.get("content") is None


# ── Manifest ──────────────────────────────────────────────────────────────────


async def test_manifest_declares_tools_and_event(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore
) -> None:
    module = build_module(tmp_index, fake_objects, storage_root="/data", tenant=TENANT)
    manifest = await module.manifest()
    tool_names = {t.name for t in manifest.tools}
    expected = {
        "storage_list",
        "storage_search",
        "storage_read",
        "storage_status",
        "storage_rescan",
        "storage_object_put",
        "storage_object_get",
    }
    assert expected <= tool_names
    assert any(e.subject == "storage.scan.completed" for e in manifest.events_emitted)


async def test_manifest_declares_files_page(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore
) -> None:
    module = build_module(tmp_index, fake_objects, storage_root="/data", tenant=TENANT)
    manifest = await module.manifest()
    page_ids = {p.id for p in manifest.pages}
    assert STORAGE_PAGE_ID in page_ids
    page = next(p for p in manifest.pages if p.id == STORAGE_PAGE_ID)
    assert page.archetype == "browser"
    assert page.title == "Files"


# ── Page data (browser archetype) ────────────────────────────────────────────


async def test_page_data_root_browse(tmp_index: FileIndex, sample_tree: Path) -> None:
    await scan(sample_tree, tmp_index, tenant=TENANT)
    entries = await tmp_index.browse(tenant=TENANT, path="")
    data = build_page_data(entries, path="", query="", download_base="/proxy/download")
    assert data["title"] == "Files"
    assert data["search_enabled"] is True
    assert data["path"] == ""
    names = {item["title"] for item in data["items"]}
    assert names == {"docs", "images"}


async def test_page_data_subdir_browse(tmp_index: FileIndex, sample_tree: Path) -> None:
    await scan(sample_tree, tmp_index, tenant=TENANT)
    entries = await tmp_index.browse(tenant=TENANT, path="docs")
    data = build_page_data(entries, path="docs", query="", download_base="/proxy/download")
    assert "docs" in data["title"]
    names = {item["title"] for item in data["items"]}
    assert "readme.txt" in names
    assert "notes.md" in names


async def test_page_data_dirs_have_nav_path(tmp_index: FileIndex, sample_tree: Path) -> None:
    await scan(sample_tree, tmp_index, tenant=TENANT)
    entries = await tmp_index.browse(tenant=TENANT, path="")
    data = build_page_data(entries, path="", query="", download_base="/proxy/download")
    dir_items = [i for i in data["items"] if i["nav_path"] is not None]
    assert dir_items, "directories must carry nav_path"
    for item in dir_items:
        assert item["href"] is None


async def test_page_data_files_have_href(tmp_index: FileIndex, sample_tree: Path) -> None:
    await scan(sample_tree, tmp_index, tenant=TENANT)
    entries = await tmp_index.browse(tenant=TENANT, path="docs")
    data = build_page_data(entries, path="docs", query="", download_base="/proxy/download")
    file_items = [i for i in data["items"] if i["nav_path"] is None]
    assert file_items, "files must carry href"
    for item in file_items:
        assert item["href"] is not None
        assert "/proxy/download" in item["href"]


async def test_page_data_search(tmp_index: FileIndex, sample_tree: Path) -> None:
    await scan(sample_tree, tmp_index, tenant=TENANT)
    entries = await tmp_index.search(tenant=TENANT, query="readme", limit=200)
    data = build_page_data(entries, path="", query="readme", download_base="/proxy/download")
    assert "readme" in data["title"].lower()
    assert len(data["items"]) == 1
    assert data["items"][0]["title"] == "readme.txt"


# ── Tenant isolation ──────────────────────────────────────────────────────────


async def test_tenant_isolation(tmp_index: FileIndex, sample_tree: Path) -> None:
    await scan(sample_tree, tmp_index, tenant="tenant-a")
    entries = await tmp_index.browse(tenant="tenant-b", path="")
    assert entries == []


# ── Privacy: notes hidden from the AGENT's file tools (#KB-refactor) ───────────


@pytest.fixture
def private_tree(tmp_path: Path) -> Path:
    """A shared tree with a public `knowledge/` and a private `notes/`."""
    (tmp_path / "knowledge" / "kb").mkdir(parents=True)
    (tmp_path / "knowledge" / "kb" / "pub.md").write_text("public knowledge")
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "secret.md").write_text("private note body")
    return tmp_path


def _names(structured: dict[str, object]) -> set[str]:
    result = structured.get("result") or []
    return {e["name"] for e in result}  # type: ignore[index,union-attr]


async def test_agent_list_hides_notes(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore, private_tree: Path
) -> None:
    await scan(private_tree, tmp_index, tenant=TENANT)
    module = build_module(
        tmp_index,
        fake_objects,
        storage_root=str(private_tree),
        tenant=TENANT,
        hidden_prefixes=("notes",),
    )
    _c, s = await module.mcp.call_tool("storage_list", {"path": ""})
    names = _names(s)  # type: ignore[arg-type]
    assert "knowledge" in names
    assert "notes" not in names


async def test_agent_cannot_browse_into_notes(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore, private_tree: Path
) -> None:
    await scan(private_tree, tmp_index, tenant=TENANT)
    module = build_module(
        tmp_index,
        fake_objects,
        storage_root=str(private_tree),
        tenant=TENANT,
        hidden_prefixes=("notes",),
    )
    _c, s = await module.mcp.call_tool("storage_list", {"path": "notes"})
    assert (s.get("result") or []) == []  # type: ignore[union-attr]


async def test_agent_search_hides_notes(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore, private_tree: Path
) -> None:
    await scan(private_tree, tmp_index, tenant=TENANT)
    module = build_module(
        tmp_index,
        fake_objects,
        storage_root=str(private_tree),
        tenant=TENANT,
        hidden_prefixes=("notes",),
    )
    _c, s = await module.mcp.call_tool("storage_search", {"query": "secret"})
    assert (s.get("result") or []) == []  # type: ignore[union-attr]


async def test_agent_read_refuses_notes(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore, private_tree: Path
) -> None:
    module = build_module(
        tmp_index,
        fake_objects,
        storage_root=str(private_tree),
        tenant=TENANT,
        hidden_prefixes=("notes",),
    )
    _c, s = await module.mcp.call_tool("storage_read", {"path": "notes/secret.md"})
    payload = s.get("result") or s  # type: ignore[union-attr]
    assert str(payload).startswith("Error:")


async def test_operator_page_still_shows_notes(tmp_index: FileIndex, private_tree: Path) -> None:
    # The Files page (operator UI) is NOT filtered — notes stay browsable there.
    await scan(private_tree, tmp_index, tenant=TENANT)
    entries = await tmp_index.browse(tenant=TENANT, path="")
    data = build_page_data(entries, path="", query="", download_base="/proxy/download")
    assert "notes" in {item["title"] for item in data["items"]}
