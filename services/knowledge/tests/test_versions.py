"""Tests for editor-save version history (#ADR-0046).

Two layers are covered:

* :class:`VersionStore` in isolation — dedup, retention, tenant isolation, get-by-id —
  against an ephemeral in-memory SQLite engine (matching ``test_db.py``).
* :class:`VaultPages` + the editor pages router — a save snapshots a version; the
  list/version endpoints return them, even when the vault is read-only.

The indexer is faked (as in ``test_pages.py``): these tests exercise the snapshot /
filesystem contract, not embeddings or Qdrant.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_core.files import FileEntry, LocalFileStore
from epicurus_knowledge.db import MAX_VERSIONS, VersionStore
from epicurus_knowledge.pages import EditorData, VaultPages, create_pages_router

TENANT = "test"
# The core file-space tenant the fake store writes under (LocalFileStore validates the id),
# distinct from the version-history tenant above. The vault is ``<tmp>/<file-tenant>/knowledge``
# and writes flow through the core file API (ADR-0064) to that same tree.
FILE_TENANT = "local"
CORE_PREFIX = "knowledge"


# ── VersionStore (the Postgres-backed history) ────────────────────────────────


@pytest.fixture
async def store() -> VersionStore:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    vs = VersionStore(engine)
    await vs.init()
    return vs


async def test_list_empty_when_no_versions(store: VersionStore) -> None:
    assert await store.list_versions(tenant=TENANT, note_path="a.md") == []


async def test_add_then_list_is_newest_first(store: VersionStore) -> None:
    await store.add_version(tenant=TENANT, note_path="a.md", title="a", content="v1")
    await store.add_version(tenant=TENANT, note_path="a.md", title="a", content="v2")
    await store.add_version(tenant=TENANT, note_path="a.md", title="a", content="v3")
    versions = await store.list_versions(tenant=TENANT, note_path="a.md")
    assert len(versions) == 3
    # Newest first: the last-saved snapshot leads. version_id is a stringified PK.
    ids = [v.version_id for v in versions]
    assert ids == sorted(ids, key=int, reverse=True)
    # Size is the character count; no body is loaded for a list row.
    newest = versions[0]
    assert newest.size == len("v3")
    assert newest.content is None
    assert isinstance(newest.created_at, datetime)


async def test_dedup_skips_byte_identical_consecutive_save(store: VersionStore) -> None:
    await store.add_version(tenant=TENANT, note_path="a.md", title="a", content="same")
    await store.add_version(tenant=TENANT, note_path="a.md", title="a", content="same")
    # An idle/blur auto-save that changed nothing must not create a duplicate row.
    assert len(await store.list_versions(tenant=TENANT, note_path="a.md")) == 1


async def test_dedup_only_against_the_newest_not_any_older(store: VersionStore) -> None:
    # A → B → A must keep all three: dedup compares only with the immediately previous one.
    await store.add_version(tenant=TENANT, note_path="a.md", title="a", content="A")
    await store.add_version(tenant=TENANT, note_path="a.md", title="a", content="B")
    await store.add_version(tenant=TENANT, note_path="a.md", title="a", content="A")
    assert len(await store.list_versions(tenant=TENANT, note_path="a.md")) == 3


async def test_retention_prunes_beyond_max(store: VersionStore) -> None:
    # Write MAX_VERSIONS + 10 distinct snapshots; only the newest MAX_VERSIONS survive.
    total = MAX_VERSIONS + 10
    for i in range(total):
        await store.add_version(tenant=TENANT, note_path="a.md", title="a", content=f"v{i}")
    versions = await store.list_versions(tenant=TENANT, note_path="a.md")
    assert len(versions) == MAX_VERSIONS
    # The surviving newest snapshot is the last one written.
    newest = await store.get_version(
        tenant=TENANT, note_path="a.md", version_id=versions[0].version_id
    )
    assert newest is not None
    assert newest.content == f"v{total - 1}"


async def test_get_version_returns_full_content(store: VersionStore) -> None:
    await store.add_version(tenant=TENANT, note_path="a.md", title="title-a", content="body-1")
    [version] = await store.list_versions(tenant=TENANT, note_path="a.md")
    fetched = await store.get_version(
        tenant=TENANT, note_path="a.md", version_id=version.version_id
    )
    assert fetched is not None
    assert fetched.content == "body-1"
    assert fetched.title == "title-a"
    assert fetched.size == len("body-1")


async def test_get_version_unknown_id_is_none(store: VersionStore) -> None:
    await store.add_version(tenant=TENANT, note_path="a.md", title="a", content="x")
    assert await store.get_version(tenant=TENANT, note_path="a.md", version_id="999999") is None


async def test_get_version_non_integer_id_is_none(store: VersionStore) -> None:
    await store.add_version(tenant=TENANT, note_path="a.md", title="a", content="x")
    # A bad query param is *not found*, never a 500.
    assert await store.get_version(tenant=TENANT, note_path="a.md", version_id="abc") is None


async def test_get_version_wrong_path_is_none(store: VersionStore) -> None:
    await store.add_version(tenant=TENANT, note_path="a.md", title="a", content="x")
    [version] = await store.list_versions(tenant=TENANT, note_path="a.md")
    # The id exists but belongs to a different document — must not leak across paths.
    assert (
        await store.get_version(tenant=TENANT, note_path="b.md", version_id=version.version_id)
        is None
    )


async def test_versions_are_tenant_isolated(store: VersionStore) -> None:
    await store.add_version(tenant="tenant-a", note_path="a.md", title="a", content="A-body")
    await store.add_version(tenant="tenant-b", note_path="a.md", title="a", content="B-body")
    a = await store.list_versions(tenant="tenant-a", note_path="a.md")
    b = await store.list_versions(tenant="tenant-b", note_path="a.md")
    assert len(a) == 1
    assert len(b) == 1
    # tenant-a must not be able to read tenant-b's snapshot by id.
    assert (
        await store.get_version(tenant="tenant-a", note_path="a.md", version_id=b[0].version_id)
        is None
    )


async def test_versions_are_path_scoped(store: VersionStore) -> None:
    await store.add_version(tenant=TENANT, note_path="a.md", title="a", content="a")
    await store.add_version(tenant=TENANT, note_path="b.md", title="b", content="b")
    assert len(await store.list_versions(tenant=TENANT, note_path="a.md")) == 1
    assert len(await store.list_versions(tenant=TENANT, note_path="b.md")) == 1


# ── VaultPages + router integration ───────────────────────────────────────────


class _FakeIndexer:
    """Records index_path calls; optionally raises to simulate an embed failure."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[str] = []
        self._fail = fail

    async def index_path(self, rel: str) -> int:
        self.calls.append(rel)
        if self._fail:
            raise RuntimeError("embed unavailable")
        return 3


class _FilePlatform:
    """A ``PlatformClient`` stand-in whose ``files_*`` hit a real on-disk ``LocalFileStore``.

    Backs the migrated ``write_doc`` so a save lands on disk (ADR-0064); only ``files_write``
    is reached by these version tests, but the full surface is provided for completeness.
    """

    def __init__(self, files_root: Path) -> None:
        self._store = LocalFileStore(files_root)

    async def files_write(self, path: str, content: str) -> FileEntry:
        return await self._store.write_text(tenant=FILE_TENANT, path=path, content=content)

    async def files_make_dir(self, path: str) -> FileEntry:
        return await self._store.ensure_dir(tenant=FILE_TENANT, path=path)

    async def files_stat(self, path: str) -> FileEntry | None:
        return await self._store.stat(tenant=FILE_TENANT, path=path)

    async def files_list(self, path: str = "") -> list[FileEntry]:
        return await self._store.list_dir(tenant=FILE_TENANT, path=path)

    async def files_delete(self, path: str) -> bool:
        return await self._store.delete(tenant=FILE_TENANT, path=path)

    async def files_move(self, src: str, dst: str) -> FileEntry:
        try:
            return await self._store.move(tenant=FILE_TENANT, src=src, dst=dst)
        except FileExistsError as exc:
            raise _status_error(409) from exc
        except FileNotFoundError as exc:
            raise _status_error(404) from exc
        except ValueError as exc:
            raise _status_error(400) from exc


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://core/platform/v1/files/move")
    return httpx.HTTPStatusError(
        f"HTTP {code}", request=request, response=httpx.Response(code, request=request)
    )


def vault_dir(files_root: Path) -> Path:
    """The on-disk vault under a fake-core files root: ``<root>/<file-tenant>/knowledge``."""
    return files_root / FILE_TENANT / CORE_PREFIX


def _make_pages(files_root: Path, indexer: object, **kw: object) -> VaultPages:
    """A fake-core-backed ``VaultPages`` over ``vault_dir(files_root)``."""
    return VaultPages(
        _vault(files_root),
        indexer,  # type: ignore[arg-type]
        platform=_FilePlatform(files_root),  # type: ignore[arg-type]
        core_prefix=CORE_PREFIX,
        **kw,  # type: ignore[arg-type]
    )


def _vault(tmp_path: Path) -> Path:
    vault = vault_dir(tmp_path)
    vault.mkdir(parents=True, exist_ok=True)
    (vault / "alpha.md").write_text("# Alpha\n", encoding="utf-8")
    return vault


async def _pages(tmp_path: Path, *, read_only: bool = False, fail: bool = False) -> VaultPages:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = VersionStore(engine)
    await store.init()
    return _make_pages(
        tmp_path,
        _FakeIndexer(fail=fail),
        read_only=read_only,
        versions=store,
        tenant=TENANT,
    )


def test_editor_data_is_versioned_by_default() -> None:
    # The shell reads this flag to enable the version-history affordance.
    assert EditorData().versioned is True


def test_list_docs_reports_versioned(tmp_path: Path) -> None:
    pages = _make_pages(tmp_path, _FakeIndexer())
    assert pages.list_docs().versioned is True


async def test_save_snapshots_a_version(tmp_path: Path) -> None:
    pages = await _pages(tmp_path)
    await pages.write_doc("alpha.md", "# Alpha edited\n")
    listing = await pages.list_versions("alpha.md")
    assert len(listing.versions) == 1
    v = listing.versions[0]
    assert v.title == "alpha"
    assert v.size == len("# Alpha edited\n")
    # created_at must be ISO-8601 parseable.
    datetime.fromisoformat(v.created_at)


async def test_multiple_saves_list_newest_first(tmp_path: Path) -> None:
    pages = await _pages(tmp_path)
    await pages.write_doc("alpha.md", "one")
    await pages.write_doc("alpha.md", "two")
    await pages.write_doc("alpha.md", "three")
    listing = await pages.list_versions("alpha.md")
    assert len(listing.versions) == 3
    # Resolve each version's content; newest (index 0) is the last write.
    newest = await pages.get_version("alpha.md", listing.versions[0].version_id)
    oldest = await pages.get_version("alpha.md", listing.versions[-1].version_id)
    assert newest.content == "three"
    assert oldest.content == "one"


async def test_identical_save_does_not_snapshot_twice(tmp_path: Path) -> None:
    pages = await _pages(tmp_path)
    await pages.write_doc("alpha.md", "same")
    await pages.write_doc("alpha.md", "same")
    listing = await pages.list_versions("alpha.md")
    assert len(listing.versions) == 1


async def test_save_snapshots_even_when_reindex_fails(tmp_path: Path) -> None:
    # The file write is the source of truth — a failed embed must still snapshot the edit.
    pages = await _pages(tmp_path, fail=True)
    result = await pages.write_doc("alpha.md", "kept-content")
    assert result.indexed is False
    listing = await pages.list_versions("alpha.md")
    assert len(listing.versions) == 1
    fetched = await pages.get_version("alpha.md", listing.versions[0].version_id)
    assert fetched.content == "kept-content"


async def test_get_version_unknown_is_404(tmp_path: Path) -> None:
    pages = await _pages(tmp_path)
    await pages.write_doc("alpha.md", "x")
    with pytest.raises(HTTPException) as err:
        await pages.get_version("alpha.md", "999999")
    assert err.value.status_code == 404


async def test_get_version_non_integer_is_404(tmp_path: Path) -> None:
    pages = await _pages(tmp_path)
    await pages.write_doc("alpha.md", "x")
    with pytest.raises(HTTPException) as err:
        await pages.get_version("alpha.md", "not-a-number")
    assert err.value.status_code == 404


async def test_list_versions_validates_path(tmp_path: Path) -> None:
    pages = await _pages(tmp_path)
    with pytest.raises(HTTPException) as err:
        await pages.list_versions("../escape.md")
    assert err.value.status_code == 400


async def test_get_version_validates_path(tmp_path: Path) -> None:
    pages = await _pages(tmp_path)
    with pytest.raises(HTTPException) as err:
        await pages.get_version("../escape.md", "1")
    assert err.value.status_code == 400


async def test_read_only_vault_still_lists_versions_but_save_is_409(tmp_path: Path) -> None:
    # A watched (read-only) vault refuses writes, so no NEW versions accrue — but the
    # history that does exist is still viewable.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = VersionStore(engine)
    await store.init()
    # Seed one historical version directly (as if written before watch mode was enabled).
    await store.add_version(tenant=TENANT, note_path="alpha.md", title="alpha", content="historic")
    indexer = _FakeIndexer()
    pages = _make_pages(tmp_path, indexer, read_only=True, versions=store, tenant=TENANT)
    # Writing is refused (no new version).
    with pytest.raises(HTTPException) as err:
        await pages.write_doc("alpha.md", "should not land")
    assert err.value.status_code == 409
    assert indexer.calls == []
    # But viewing the existing history is allowed.
    listing = await pages.list_versions("alpha.md")
    assert len(listing.versions) == 1
    fetched = await pages.get_version("alpha.md", listing.versions[0].version_id)
    assert fetched.content == "historic"


async def test_versions_disabled_when_store_absent(tmp_path: Path) -> None:
    # Without a store (the bare test wiring), the editor still works; history is empty.
    pages = _make_pages(tmp_path, _FakeIndexer())
    await pages.write_doc("alpha.md", "no-history")
    assert (await pages.list_versions("alpha.md")).versions == []
    with pytest.raises(HTTPException) as err:
        await pages.get_version("alpha.md", "1")
    assert err.value.status_code == 404


# ── router (the HTTP surface the core proxies) ────────────────────────────────


async def _client(tmp_path: Path, *, read_only: bool = False) -> TestClient:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = VersionStore(engine)
    await store.init()
    app = FastAPI()
    app.include_router(
        create_pages_router(
            _make_pages(
                tmp_path,
                _FakeIndexer(),
                read_only=read_only,
                versions=store,
                tenant=TENANT,
            )
        )
    )
    return TestClient(app)


async def test_router_lists_and_fetches_versions(tmp_path: Path) -> None:
    client = await _client(tmp_path)
    client.put("/pages/vault/doc", params={"path": "alpha.md"}, json={"content": "first"})
    client.put("/pages/vault/doc", params={"path": "alpha.md"}, json={"content": "second"})

    listing = client.get("/pages/vault/doc/versions", params={"path": "alpha.md"})
    assert listing.status_code == 200
    versions = listing.json()["versions"]
    assert len(versions) == 2
    # Newest first.
    assert versions[0]["size"] == len("second")

    got = client.get(
        "/pages/vault/doc/version",
        params={"path": "alpha.md", "version": versions[0]["version_id"]},
    )
    assert got.status_code == 200
    body = got.json()
    assert body["content"] == "second"
    assert body["path"] == "alpha.md"
    assert body["version_id"] == versions[0]["version_id"]


async def test_router_unknown_version_is_404(tmp_path: Path) -> None:
    client = await _client(tmp_path)
    client.put("/pages/vault/doc", params={"path": "alpha.md"}, json={"content": "x"})
    resp = client.get("/pages/vault/doc/version", params={"path": "alpha.md", "version": "999999"})
    assert resp.status_code == 404


async def test_router_version_list_path_traversal_is_400(tmp_path: Path) -> None:
    client = await _client(tmp_path)
    resp = client.get("/pages/vault/doc/versions", params={"path": "../x.md"})
    assert resp.status_code == 400


async def test_router_unknown_page_for_versions_is_404(tmp_path: Path) -> None:
    client = await _client(tmp_path)
    resp = client.get("/pages/ghost/doc/versions", params={"path": "alpha.md"})
    assert resp.status_code == 404


async def test_router_read_only_vault_lists_versions(tmp_path: Path) -> None:
    # History is viewable through the router even when the vault is read-only.
    client = await _client(tmp_path, read_only=True)
    # A write is refused (watch mode), so seed a version via a non-read-only path is not
    # possible here; instead assert the listing endpoint itself is reachable (empty list,
    # 200 — not 409). Viewing history must never be gated behind write access.
    listing = client.get("/pages/vault/doc/versions", params={"path": "alpha.md"})
    assert listing.status_code == 200
    assert listing.json()["versions"] == []
