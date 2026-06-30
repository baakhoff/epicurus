"""Unit tests for the storage MCP tools (file-space contract, ADR-0063).

After the file-space migration the **core** owns the unified file space and serves the Files
browser; storage is a consumer of it (via :class:`epicurus_core.PlatformClient`) plus the owner
of the MinIO object store. These tests therefore exercise the agent file tools against:

  * a **fake PlatformClient** standing in for the core file space (``files_list`` /
    ``files_search`` / ``files_read``), and
  * an in-process SQLite :class:`FileIndex` + in-memory object store for the module's own
    catalogued objects.

The merge behaviour (file-space + objects), the hidden-prefix gate (``notes`` is private to the
agent), the ``storage_read`` resolution order (object first, then file space) and its httpx
error mapping are all covered here. The object-store catalogue plumbing (ingest / put / move /
download) lives in test_storage_ingest.py and test_storage_move.py.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import cast

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_core import EpicurusModule, FileEntry, FileKind, PlatformClient
from epicurus_storage.db import FileIndex
from epicurus_storage.object_store import ObjectStore, StoredObject
from epicurus_storage.service import build_module

TENANT = "test"


def _build(
    index: FileIndex,
    objects: ObjectStore,
    *,
    platform: object,
    tenant: str = TENANT,
    hidden_prefixes: tuple[str, ...] = (),
) -> EpicurusModule:
    """Build the module with a duck-typed platform stub (cast at the seam, per repo convention)."""
    return build_module(
        index,
        objects,
        platform=cast(PlatformClient, platform),
        tenant=tenant,
        hidden_prefixes=hidden_prefixes,
    )


# ── Fakes ──────────────────────────────────────────────────────────────────────


class _FakePlatform:
    """Stand-in for the core-owned file space (the ``PlatformClient.files_*`` surface).

    ``files_list`` / ``files_search`` return seeded :class:`epicurus_core.FileEntry` rows;
    ``files_read`` returns seeded text or raises an ``httpx`` error so the tool's error mapping
    can be exercised. ``raise_on`` lets a test force a transport-level failure (no
    ``HTTPStatusError`` response) to assert the "objects only" / "file space unavailable" paths.
    """

    def __init__(
        self,
        *,
        listing: dict[str, list[FileEntry]] | None = None,
        search_hits: list[FileEntry] | None = None,
        reads: dict[str, str] | None = None,
        read_errors: dict[str, int] | None = None,
        raise_on: set[str] | None = None,
    ) -> None:
        self._listing = listing or {}
        self._search_hits = search_hits or []
        self._reads = reads or {}
        self._read_errors = read_errors or {}
        self._raise_on = raise_on or set()
        self.list_calls: list[str] = []
        self.search_calls: list[tuple[str, int]] = []
        self.read_calls: list[str] = []

    async def files_list(self, path: str = "") -> list[FileEntry]:
        self.list_calls.append(path)
        if "list" in self._raise_on:
            raise httpx.ConnectError("core down")
        return list(self._listing.get(path, []))

    async def files_search(self, query: str, *, limit: int = 50) -> list[FileEntry]:
        self.search_calls.append((query, limit))
        if "search" in self._raise_on:
            raise httpx.ConnectError("core down")
        return list(self._search_hits)

    async def files_read(self, path: str) -> str:
        self.read_calls.append(path)
        if "read" in self._raise_on:
            raise httpx.ConnectError("core down")
        if path in self._read_errors:
            raise httpx.HTTPStatusError(
                "boom",
                request=httpx.Request("GET", "http://core/files/read"),
                response=httpx.Response(self._read_errors[path]),
            )
        return self._reads[path]


class _FakeObjectStore(ObjectStore):
    """In-memory object store (binary surface) — no MinIO required."""

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

    async def put(self, *, tenant: str, key: str, content: str) -> None:
        await self.put_bytes(
            tenant=tenant, key=key, data=content.encode("utf-8"), content_type="text/plain"
        )

    async def get(self, *, tenant: str, key: str) -> str | None:
        stored = self._mem.get(self._k(tenant, key))
        return None if stored is None else stored.data.decode("utf-8")


def _entry(path: str, kind: FileKind = "file", size: int = 0) -> FileEntry:
    return FileEntry(path=path, name=path.rsplit("/", 1)[-1], kind=kind, size=size)


# ── Fixtures ────────────────────────────────────────────────────────────────────


@pytest.fixture
async def tmp_index() -> AsyncIterator[FileIndex]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    idx = FileIndex(engine)
    await idx.init()
    yield idx
    await engine.dispose()


@pytest.fixture
def fake_objects() -> _FakeObjectStore:
    return _FakeObjectStore()


async def _seed_objects(
    index: FileIndex, objects: _FakeObjectStore, key: str, content: str
) -> None:
    """Catalogue an object (and its bytes) the way ``put_object`` would."""
    from epicurus_storage.service import put_object

    await put_object(index=index, objects=objects, tenant=TENANT, key=key, content=content)


def _result(structured: object) -> object:
    assert isinstance(structured, dict)
    return structured.get("result", structured)


def _names(structured: object) -> set[str]:
    rows = _result(structured)
    assert isinstance(rows, list)
    return {row["name"] for row in rows}


def _paths(structured: object) -> list[str]:
    rows = _result(structured)
    assert isinstance(rows, list)
    return [row["path"] for row in rows]


# ── File-index helpers (object rows only — no more filesystem scan) ──────────────


async def test_browse_and_count_over_objects(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore
) -> None:
    await _seed_objects(tmp_index, fake_objects, "docs/readme.md", "0123456789")
    await _seed_objects(tmp_index, fake_objects, "images/photo.txt", "img")

    root = {e.name for e in await tmp_index.browse(tenant=TENANT, path="")}
    assert root == {"docs", "images"}
    docs = {e.name for e in await tmp_index.browse(tenant=TENANT, path="docs")}
    assert docs == {"readme.md"}

    counts = await tmp_index.count(tenant=TENANT)
    assert counts["files"] == 2  # readme.md, photo.txt
    assert counts["dirs"] == 2  # docs/, images/


async def test_index_search_by_name(tmp_index: FileIndex, fake_objects: _FakeObjectStore) -> None:
    await _seed_objects(tmp_index, fake_objects, "docs/readme.md", "x")
    hits = await tmp_index.search(tenant=TENANT, query="README")  # case-insensitive
    assert any(h.name == "readme.md" for h in hits)


async def test_tenant_isolation(tmp_index: FileIndex, fake_objects: _FakeObjectStore) -> None:
    await _seed_objects(tmp_index, fake_objects, "a.md", "x")
    assert await tmp_index.browse(tenant="other-tenant", path="") == []


# ── storage_list: merge file-space + objects, dirs first by name ────────────────


async def test_list_merges_filespace_and_objects(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore
) -> None:
    # File space contributes a dir + a file; the object store contributes an "uploads" dir.
    platform = _FakePlatform(
        listing={"": [_entry("knowledge", "dir"), _entry("z-top.md", "file", 4)]}
    )
    await _seed_objects(tmp_index, fake_objects, "uploads/report.md", "hi")
    module = _build(tmp_index, fake_objects, platform=platform, tenant=TENANT)

    _c, s = await module.mcp.call_tool("storage_list", {"path": ""})
    # Both sources are present.
    assert _names(s) == {"knowledge", "uploads", "z-top.md"}
    # Dirs precede files, each group sorted by lower-cased name.
    assert _paths(s) == ["knowledge", "uploads", "z-top.md"]
    assert platform.list_calls == [""]


async def test_list_default_path_is_root(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore
) -> None:
    platform = _FakePlatform(listing={"": [_entry("a.md", "file")]})
    module = _build(tmp_index, fake_objects, platform=platform, tenant=TENANT)
    _c, s = await module.mcp.call_tool("storage_list", {})  # default path=""
    assert _names(s) == {"a.md"}
    assert platform.list_calls == [""]


async def test_list_subdir_forwards_path(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore
) -> None:
    platform = _FakePlatform(listing={"docs": [_entry("docs/guide.md", "file")]})
    module = _build(tmp_index, fake_objects, platform=platform, tenant=TENANT)
    _c, s = await module.mcp.call_tool("storage_list", {"path": "docs"})
    assert _names(s) == {"guide.md"}
    assert platform.list_calls == ["docs"]


async def test_list_tolerates_core_down_returns_objects_only(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore
) -> None:
    # The platform raises a transport error → the tool degrades to object rows, never errors.
    platform = _FakePlatform(raise_on={"list"})
    await _seed_objects(tmp_index, fake_objects, "uploads/x.md", "x")
    module = _build(tmp_index, fake_objects, platform=platform, tenant=TENANT)
    _c, s = await module.mcp.call_tool("storage_list", {"path": ""})
    assert _names(s) == {"uploads"}


# ── storage_search: merge file-space + objects, capped ──────────────────────────


async def test_search_merges_and_caps(tmp_index: FileIndex, fake_objects: _FakeObjectStore) -> None:
    platform = _FakePlatform(search_hits=[_entry("docs/quarterly.md", "file")])
    await _seed_objects(tmp_index, fake_objects, "quarterly-upload.md", "x")
    module = _build(tmp_index, fake_objects, platform=platform, tenant=TENANT)

    _c, s = await module.mcp.call_tool("storage_search", {"query": "quarterly"})
    assert _names(s) == {"quarterly.md", "quarterly-upload.md"}
    # The capped limit (default 50) is forwarded to the platform.
    assert platform.search_calls == [("quarterly", 50)]


async def test_search_limit_is_clamped_to_200(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore
) -> None:
    platform = _FakePlatform()
    module = _build(tmp_index, fake_objects, platform=platform, tenant=TENANT)
    await module.mcp.call_tool("storage_search", {"query": "q", "limit": 9999})
    assert platform.search_calls == [("q", 200)]


async def test_search_empty_query_returns_empty(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore
) -> None:
    platform = _FakePlatform(search_hits=[_entry("x.md")])
    module = _build(tmp_index, fake_objects, platform=platform, tenant=TENANT)
    _c, s = await module.mcp.call_tool("storage_search", {"query": "   "})
    assert _result(s) == []
    assert platform.search_calls == []  # short-circuits before hitting the core


async def test_search_tolerates_core_down(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore
) -> None:
    platform = _FakePlatform(raise_on={"search"})
    await _seed_objects(tmp_index, fake_objects, "obj-hit.md", "x")
    module = _build(tmp_index, fake_objects, platform=platform, tenant=TENANT)
    _c, s = await module.mcp.call_tool("storage_search", {"query": "hit"})
    assert _names(s) == {"obj-hit.md"}


# ── storage_read: object store first, then the file space ───────────────────────


async def test_read_filespace_text(tmp_index: FileIndex, fake_objects: _FakeObjectStore) -> None:
    platform = _FakePlatform(reads={"docs/readme.txt": "0123456789"})
    module = _build(tmp_index, fake_objects, platform=platform, tenant=TENANT)
    _c, s = await module.mcp.call_tool("storage_read", {"path": "docs/readme.txt"})
    assert _result(s) == "0123456789"
    assert platform.read_calls == ["docs/readme.txt"]


async def test_read_prefers_object_over_filespace(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore
) -> None:
    # An agent-written object shadows any file-space file at the same path: object wins, and the
    # core file API is never consulted.
    await _seed_objects(tmp_index, fake_objects, "report.md", "from-object")
    platform = _FakePlatform(reads={"report.md": "from-filespace"})
    module = _build(tmp_index, fake_objects, platform=platform, tenant=TENANT)
    _c, s = await module.mcp.call_tool("storage_read", {"path": "report.md"})
    assert _result(s) == "from-object"
    assert platform.read_calls == []  # object short-circuits the file-space read


async def test_read_object_binary_rejected(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore
) -> None:
    # A catalogued object whose bytes are not valid UTF-8 is rejected by the tool.
    await fake_objects.put_bytes(
        tenant=TENANT, key="blob.bin", data=b"\xff\xfe\x00", content_type="application/octet-stream"
    )
    await tmp_index.upsert_batch(
        tenant=TENANT,
        source="object",
        entries=[{"path": "blob.bin", "name": "blob.bin", "size": 3, "mtime": 0.0, "kind": "file"}],
    )
    platform = _FakePlatform()
    module = _build(tmp_index, fake_objects, platform=platform, tenant=TENANT)
    _c, s = await module.mcp.call_tool("storage_read", {"path": "blob.bin"})
    assert str(_result(s)).startswith("Error:")
    assert "UTF-8" in str(_result(s))


async def test_read_object_too_large_rejected(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore
) -> None:
    big = b"x" * (256 * 1024 + 1)
    await fake_objects.put_bytes(tenant=TENANT, key="big.txt", data=big, content_type="text/plain")
    await tmp_index.upsert_batch(
        tenant=TENANT,
        source="object",
        entries=[
            {"path": "big.txt", "name": "big.txt", "size": len(big), "mtime": 0.0, "kind": "file"}
        ],
    )
    platform = _FakePlatform()
    module = _build(tmp_index, fake_objects, platform=platform, tenant=TENANT)
    _c, s = await module.mcp.call_tool("storage_read", {"path": "big.txt"})
    assert str(_result(s)).startswith("Error:")
    assert "too large" in str(_result(s))


async def test_read_maps_filespace_404(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore
) -> None:
    platform = _FakePlatform(read_errors={"docs/nope.txt": 404})
    module = _build(tmp_index, fake_objects, platform=platform, tenant=TENANT)
    _c, s = await module.mcp.call_tool("storage_read", {"path": "docs/nope.txt"})
    assert _result(s) == "Error: file not found"


async def test_read_maps_filespace_413(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore
) -> None:
    platform = _FakePlatform(read_errors={"docs/huge.txt": 413})
    module = _build(tmp_index, fake_objects, platform=platform, tenant=TENANT)
    _c, s = await module.mcp.call_tool("storage_read", {"path": "docs/huge.txt"})
    payload = str(_result(s))
    assert payload.startswith("Error:") and "too large" in payload


async def test_read_maps_filespace_415(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore
) -> None:
    platform = _FakePlatform(read_errors={"docs/blob.bin": 415})
    module = _build(tmp_index, fake_objects, platform=platform, tenant=TENANT)
    _c, s = await module.mcp.call_tool("storage_read", {"path": "docs/blob.bin"})
    payload = str(_result(s))
    assert payload.startswith("Error:") and "UTF-8" in payload


async def test_read_maps_other_status_code(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore
) -> None:
    platform = _FakePlatform(read_errors={"docs/x.txt": 500})
    module = _build(tmp_index, fake_objects, platform=platform, tenant=TENANT)
    _c, s = await module.mcp.call_tool("storage_read", {"path": "docs/x.txt"})
    assert _result(s) == "Error: read failed (HTTP 500)"


async def test_read_maps_transport_error_to_unavailable(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore
) -> None:
    platform = _FakePlatform(raise_on={"read"})
    module = _build(tmp_index, fake_objects, platform=platform, tenant=TENANT)
    _c, s = await module.mcp.call_tool("storage_read", {"path": "docs/x.txt"})
    assert _result(s) == "Error: file space unavailable"


# ── storage_status: object-store entry counts ───────────────────────────────────


async def test_status_reports_object_counts(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore
) -> None:
    await _seed_objects(tmp_index, fake_objects, "docs/a.md", "x")  # 1 dir + 1 file
    await _seed_objects(tmp_index, fake_objects, "b.md", "y")  # 1 file
    platform = _FakePlatform()
    module = _build(tmp_index, fake_objects, platform=platform, tenant=TENANT)
    _c, s = await module.mcp.call_tool("storage_status", {})
    payload = _result(s)
    assert isinstance(payload, dict)
    assert payload == {"object_files": 2, "object_dirs": 1}


# ── storage_object_put / storage_object_get ─────────────────────────────────────


async def test_object_put_then_get(tmp_index: FileIndex, fake_objects: _FakeObjectStore) -> None:
    platform = _FakePlatform()
    module = _build(tmp_index, fake_objects, platform=platform, tenant=TENANT)

    _c, put_result = await module.mcp.call_tool(
        "storage_object_put", {"key": "report.txt", "content": "hello world"}
    )
    put_payload = _result(put_result)
    assert isinstance(put_payload, dict)
    assert put_payload == {"status": "ok", "key": "report.txt"}

    _c, get_result = await module.mcp.call_tool("storage_object_get", {"key": "report.txt"})
    get_payload = _result(get_result)
    assert isinstance(get_payload, dict)
    assert get_payload.get("content") == "hello world"


async def test_object_get_missing_is_null(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore
) -> None:
    platform = _FakePlatform()
    module = _build(tmp_index, fake_objects, platform=platform, tenant=TENANT)
    _c, get_result = await module.mcp.call_tool("storage_object_get", {"key": "does-not-exist.txt"})
    get_payload = _result(get_result)
    assert isinstance(get_payload, dict)
    assert get_payload.get("content") is None


async def test_object_put_appears_in_list_and_reads_back(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore
) -> None:
    # End-to-end through the tools: put an object, then it lists and reads back.
    platform = _FakePlatform()
    module = _build(tmp_index, fake_objects, platform=platform, tenant=TENANT)
    await module.mcp.call_tool(
        "storage_object_put", {"key": "memo.md", "content": "agent wrote it"}
    )

    _c, listed = await module.mcp.call_tool("storage_list", {"path": ""})
    assert "memo.md" in _names(listed)

    _c, read = await module.mcp.call_tool("storage_read", {"path": "memo.md"})
    assert _result(read) == "agent wrote it"


# ── Privacy: notes hidden from the AGENT's file tools (#KB-refactor) ─────────────


async def test_list_hides_hidden_prefix(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore
) -> None:
    # The core file space exposes both a public and a private (notes) subtree; the agent's
    # list tool must drop the private one. Objects under notes/ are hidden too.
    platform = _FakePlatform(listing={"": [_entry("knowledge", "dir"), _entry("notes", "dir")]})
    await _seed_objects(tmp_index, fake_objects, "notes/secret-obj.md", "x")
    await _seed_objects(tmp_index, fake_objects, "uploads/ok.md", "y")
    module = _build(tmp_index, fake_objects, platform=platform, hidden_prefixes=("notes",))
    _c, s = await module.mcp.call_tool("storage_list", {"path": ""})
    names = _names(s)
    assert "knowledge" in names
    assert "uploads" in names
    assert "notes" not in names


async def test_cannot_browse_into_hidden_prefix(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore
) -> None:
    platform = _FakePlatform(listing={"notes": [_entry("notes/secret.md", "file")]})
    module = _build(tmp_index, fake_objects, platform=platform, hidden_prefixes=("notes",))
    _c, s = await module.mcp.call_tool("storage_list", {"path": "notes"})
    assert _result(s) == []
    assert platform.list_calls == []  # short-circuits before hitting the core


async def test_search_hides_hidden_prefix(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore
) -> None:
    platform = _FakePlatform(search_hits=[_entry("notes/secret.md", "file")])
    await _seed_objects(tmp_index, fake_objects, "notes/secret-obj.md", "x")
    module = _build(tmp_index, fake_objects, platform=platform, hidden_prefixes=("notes",))
    _c, s = await module.mcp.call_tool("storage_search", {"query": "secret"})
    assert _result(s) == []


async def test_read_refuses_hidden_prefix(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore
) -> None:
    # Even if an object exists under notes/, the agent's read tool refuses before touching it.
    await _seed_objects(tmp_index, fake_objects, "notes/secret.md", "private note body")
    platform = _FakePlatform(reads={"notes/secret.md": "private note body"})
    module = _build(tmp_index, fake_objects, platform=platform, hidden_prefixes=("notes",))
    _c, s = await module.mcp.call_tool("storage_read", {"path": "notes/secret.md"})
    assert _result(s) == "Error: not available"
    assert platform.read_calls == []


# ── Manifest ────────────────────────────────────────────────────────────────────


async def test_manifest_declares_the_tools(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore
) -> None:
    module = _build(tmp_index, fake_objects, platform=_FakePlatform(), tenant=TENANT)
    manifest = await module.manifest()
    tool_names = {t.name for t in manifest.tools}
    assert tool_names == {
        "storage_list",
        "storage_search",
        "storage_read",
        "storage_status",
        "storage_object_put",
        "storage_object_get",
    }


async def test_manifest_declares_no_pages_or_events(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore
) -> None:
    # The Files page moved to the core (ADR-0063); the module no longer ships a page or events.
    module = _build(tmp_index, fake_objects, platform=_FakePlatform(), tenant=TENANT)
    manifest = await module.manifest()
    assert manifest.pages == []
    assert manifest.events_emitted == []


async def test_manifest_status_action_present(
    tmp_index: FileIndex, fake_objects: _FakeObjectStore
) -> None:
    # The UI keeps a single "Show status" action; the old "Re-scan now" action is gone.
    module = _build(tmp_index, fake_objects, platform=_FakePlatform(), tenant=TENANT)
    manifest = await module.manifest()
    assert manifest.ui is not None
    action_tools = {a.tool for a in manifest.ui.actions}
    assert action_tools == {"storage_status"}
