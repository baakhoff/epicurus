"""Integration tests for external file mounts over the Files platform API (#731).

Exercised through the real ASGI app (router + the app-level PathEscapeError handler, mirroring
production wiring in app.py) against real ``LocalFileStore`` instances and a real ``FileIndex``
(in-memory SQLite) — the tenant space and each mount are genuinely separate directory trees.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core.files import LocalFileStore, PathEscapeError
from epicurus_core_app.file_index import FileIndex
from epicurus_core_app.file_scan import scan
from epicurus_core_app.files_routes import create_files_router
from epicurus_core_app.mounts import Mount, MountSpec, build_mounts
from epicurus_core_app.object_backend import ObjectDownload, ObjectEntry, ObjectText

TENANT = "local"
PAGE = "/platform/v1/files/page"
LIST = "/platform/v1/files/list"
READ = "/platform/v1/files/read"
STAT = "/platform/v1/files/stat"
WRITE = "/platform/v1/files/write"
DELETE = "/platform/v1/files"
DIR = "/platform/v1/files/dir"
MOVE = "/platform/v1/files/move"
UPLOAD = "/platform/v1/files/upload"
ENTRY = "/platform/v1/files/entry"
SEARCH = "/platform/v1/files/search"
DOWNLOAD = "/platform/v1/files/download"


class _AlwaysHitObjects:
    """An object backend that answers every call — proves a mount path never falls through
    to the object store (#731): if it ever did, these sentinel values would leak through."""

    async def list(self, *, tenant: str, path: str, query: str) -> list[ObjectEntry]:
        return [ObjectEntry(path="SHOULD-NOT-APPEAR", name="SHOULD-NOT-APPEAR", size=1)]

    async def read(self, *, tenant: str, path: str) -> ObjectText | None:
        return ObjectText(path=path, name="SHOULD-NOT-APPEAR", content="SHOULD-NOT-APPEAR")

    async def download(self, *, tenant: str, path: str) -> ObjectDownload | None:
        async def _gen() -> AsyncIterator[bytes]:
            yield b"SHOULD-NOT-APPEAR"

        return ObjectDownload(name="SHOULD-NOT-APPEAR", content_type="text/plain", body=_gen())

    async def delete(self, *, tenant: str, path: str) -> bool:
        return True

    async def move(self, *, tenant: str, src: str, dst: str) -> ObjectEntry:
        return ObjectEntry(path=dst, name="SHOULD-NOT-APPEAR", size=1)


def _symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError as exc:
        pytest.skip(f"cannot create symlinks in this environment: {exc}")


async def _make_client(
    tmp_path: Path,
    *,
    mount_specs: list[MountSpec],
    with_objects: bool = False,
) -> tuple[AsyncClient, FileIndex, dict[str, Mount]]:
    tenant_root = tmp_path / "tenant"
    tenant_root.mkdir()
    (tenant_root / TENANT).mkdir()
    (tenant_root / TENANT / "top.txt").write_text("tenant-file", encoding="utf-8")
    store = LocalFileStore(tenant_root)

    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    index = FileIndex(engine)
    await index.init()
    await scan(store, index, tenant=TENANT)

    mounts = build_mounts(mount_specs)
    for mount in mounts.values():
        if mount.indexed:
            await scan(
                mount.store,
                index,
                tenant=TENANT,
                path_prefix=f"mount:{mount.name}/",
                exclude=mount.exclude,
            )

    app = FastAPI()
    app.include_router(
        create_files_router(
            store,
            default_tenant=TENANT,
            index=index,
            objects=_AlwaysHitObjects() if with_objects else None,
            mounts=mounts,
        )
    )

    # Mirror app.py's production wiring exactly (#731): without this handler a symlink
    # escape inside a mount would 500, not the clean 400 the acceptance criteria call for.
    @app.exception_handler(PathEscapeError)
    async def _on_path_escape(_request: Request, exc: PathEscapeError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    # Mirror app.py's production wiring exactly (#731): a mount is host content the core
    # doesn't control, so an OS-level failure (permission denied, disk full) is realistic in
    # a way it isn't for the core-managed tenant space — this turns a bare, undiagnosable 500
    # into one that names the underlying OS error.
    @app.exception_handler(OSError)
    async def _on_os_error(_request: Request, exc: OSError) -> JSONResponse:
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    client = AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    )
    return client, index, mounts


def _ro_mount(tmp_path: Path, name: str = "media") -> tuple[Path, MountSpec]:
    root = tmp_path / name
    root.mkdir()
    (root / "photo.jpg").write_text("binary-ish", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "nested.txt").write_text("nested", encoding="utf-8")
    return root, MountSpec(name=name, path=root, read_only=True, indexed=False, exclude=())


def _rw_mount(tmp_path: Path, name: str = "docs") -> tuple[Path, MountSpec]:
    root = tmp_path / name
    root.mkdir()
    (root / "report.md").write_text("draft", encoding="utf-8")
    return root, MountSpec(name=name, path=root, read_only=False, indexed=False, exclude=())


# ── root page: mounts as top-level roots ────────────────────────────────────────


async def test_root_page_lists_declared_mounts_as_dirs(tmp_path: Path) -> None:
    _, ro_spec = _ro_mount(tmp_path)
    _, rw_spec = _rw_mount(tmp_path)
    client, _, _ = await _make_client(tmp_path, mount_specs=[ro_spec, rw_spec])
    async with client:
        data = (await client.get(PAGE)).json()
    by_title = {it["title"]: it for it in data["items"]}
    assert "media" in by_title and "docs" in by_title
    assert by_title["media"]["nav_path"] == "mount:media"
    assert by_title["media"]["icon"] == "folder"
    assert by_title["media"]["movable"] is False
    assert by_title["media"]["deletable"] is False
    assert data["read_only"] is False  # the root itself is never read-only


async def test_no_mounts_declared_root_page_unaffected(tmp_path: Path) -> None:
    client, _, _ = await _make_client(tmp_path, mount_specs=[])
    async with client:
        data = (await client.get(PAGE)).json()
    assert [it["title"] for it in data["items"]] == ["top.txt"]


# ── browsing into a mount ────────────────────────────────────────────────────────


async def test_browse_into_ro_mount_lists_children_read_only(tmp_path: Path) -> None:
    _, ro_spec = _ro_mount(tmp_path)
    client, _, _ = await _make_client(tmp_path, mount_specs=[ro_spec])
    async with client:
        data = (await client.get(PAGE, params={"path": "mount:media"})).json()
    assert data["read_only"] is True
    by_title = {it["title"]: it for it in data["items"]}
    assert by_title["photo.jpg"]["movable"] is False
    assert by_title["photo.jpg"]["deletable"] is False
    assert by_title["sub"]["nav_path"] == "mount:media/sub"
    assert by_title["sub"]["deletable"] is False


async def test_browse_into_rw_mount_lists_children_writable(tmp_path: Path) -> None:
    _, rw_spec = _rw_mount(tmp_path)
    client, _, _ = await _make_client(tmp_path, mount_specs=[rw_spec])
    async with client:
        data = (await client.get(PAGE, params={"path": "mount:docs"})).json()
    assert data["read_only"] is False
    by_title = {it["title"]: it for it in data["items"]}
    assert by_title["report.md"]["movable"] is True
    assert by_title["report.md"]["deletable"] is True


async def test_browse_into_mount_subdir(tmp_path: Path) -> None:
    _, ro_spec = _ro_mount(tmp_path)
    client, _, _ = await _make_client(tmp_path, mount_specs=[ro_spec])
    async with client:
        data = (await client.get(PAGE, params={"path": "mount:media/sub"})).json()
    assert [it["title"] for it in data["items"]] == ["nested.txt"]
    assert data["read_only"] is True


# ── list / read / stat ───────────────────────────────────────────────────────────


async def test_list_files_reprefixes_entries(tmp_path: Path) -> None:
    _, ro_spec = _ro_mount(tmp_path)
    client, _, _ = await _make_client(tmp_path, mount_specs=[ro_spec])
    async with client:
        resp = await client.get(LIST, params={"path": "mount:media"})
    assert resp.status_code == 200
    paths = {e["path"] for e in resp.json()["entries"]}
    assert paths == {"mount:media/photo.jpg", "mount:media/sub"}


async def test_read_file_in_mount(tmp_path: Path) -> None:
    _, rw_spec = _rw_mount(tmp_path)
    client, _, _ = await _make_client(tmp_path, mount_specs=[rw_spec])
    async with client:
        resp = await client.get(READ, params={"path": "mount:docs/report.md"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "draft"
    assert body["path"] == "mount:docs/report.md"


async def test_read_missing_file_in_mount_404s_no_object_fallback(tmp_path: Path) -> None:
    _, rw_spec = _rw_mount(tmp_path)
    client, _, _ = await _make_client(tmp_path, mount_specs=[rw_spec], with_objects=True)
    async with client:
        resp = await client.get(READ, params={"path": "mount:docs/ghost.md"})
    assert resp.status_code == 404  # not the _AlwaysHitObjects sentinel


async def test_stat_file_in_mount(tmp_path: Path) -> None:
    _, ro_spec = _ro_mount(tmp_path)
    client, _, _ = await _make_client(tmp_path, mount_specs=[ro_spec])
    async with client:
        resp = await client.get(STAT, params={"path": "mount:media/photo.jpg"})
    assert resp.status_code == 200
    assert resp.json()["path"] == "mount:media/photo.jpg"


# ── RO enforcement (write / delete / dir / upload / move) ───────────────────────


async def test_write_into_ro_mount_refused(tmp_path: Path) -> None:
    _, ro_spec = _ro_mount(tmp_path)
    client, _, _ = await _make_client(tmp_path, mount_specs=[ro_spec])
    async with client:
        resp = await client.put(
            WRITE, params={"path": "mount:media/new.txt"}, json={"content": "x"}
        )
    assert resp.status_code == 403


async def test_delete_from_ro_mount_refused(tmp_path: Path) -> None:
    _, ro_spec = _ro_mount(tmp_path)
    client, _, _ = await _make_client(tmp_path, mount_specs=[ro_spec])
    async with client:
        resp = await client.delete(DELETE, params={"path": "mount:media/photo.jpg"})
    assert resp.status_code == 403


async def test_mkdir_in_ro_mount_refused(tmp_path: Path) -> None:
    _, ro_spec = _ro_mount(tmp_path)
    client, _, _ = await _make_client(tmp_path, mount_specs=[ro_spec])
    async with client:
        resp = await client.post(DIR, params={"path": "mount:media/newdir"})
    assert resp.status_code == 403


async def test_upload_into_ro_mount_refused(tmp_path: Path) -> None:
    _, ro_spec = _ro_mount(tmp_path)
    client, _, _ = await _make_client(tmp_path, mount_specs=[ro_spec])
    async with client:
        resp = await client.post(
            UPLOAD,
            params={"dir": "mount:media"},
            files={"file": ("new.txt", b"hi", "text/plain")},
        )
    assert resp.status_code == 403


async def test_delete_entry_from_ro_mount_refused(tmp_path: Path) -> None:
    _, ro_spec = _ro_mount(tmp_path)
    client, _, _ = await _make_client(tmp_path, mount_specs=[ro_spec])
    async with client:
        resp = await client.delete(ENTRY, params={"path": "mount:media/photo.jpg"})
    assert resp.status_code == 403


async def test_move_within_ro_mount_refused(tmp_path: Path) -> None:
    _, ro_spec = _ro_mount(tmp_path)
    client, _, _ = await _make_client(tmp_path, mount_specs=[ro_spec])
    async with client:
        resp = await client.post(
            MOVE, json={"src": "mount:media/photo.jpg", "dst": "mount:media/renamed.jpg"}
        )
    assert resp.status_code == 403


# ── RW mount: mutations actually work ────────────────────────────────────────────


async def test_write_into_rw_mount_succeeds_and_persists(tmp_path: Path) -> None:
    root, rw_spec = _rw_mount(tmp_path)
    client, _, _ = await _make_client(tmp_path, mount_specs=[rw_spec])
    async with client:
        resp = await client.put(
            WRITE, params={"path": "mount:docs/new.txt"}, json={"content": "hello"}
        )
    assert resp.status_code == 200
    assert resp.json()["path"] == "mount:docs/new.txt"
    assert (root / "new.txt").read_text(encoding="utf-8") == "hello"


async def test_os_error_writing_to_a_mount_is_a_diagnosable_500(tmp_path: Path) -> None:
    """A mount is host content the core doesn't control — an OS-level write failure is a
    realistic misconfiguration (declared `rw` but the host path isn't actually writable,
    #731), and must surface *why*, not crash opaquely.

    Reproduced portably (no chmod — Windows doesn't enforce POSIX permission bits the same
    way CI's Linux runner does, which is exactly how this shipped broken the first time,
    caught only in CI): declare the mount's root as an existing *file*, not a directory, so
    the write's ``target.parent.mkdir(...)`` genuinely raises an ``OSError``.
    """
    not_a_dir = tmp_path / "docs-is-actually-a-file"
    not_a_dir.write_text("oops", encoding="utf-8")
    spec = MountSpec(name="docs", path=not_a_dir, read_only=False, indexed=False, exclude=())
    client, _, _ = await _make_client(tmp_path, mount_specs=[spec])
    async with client:
        resp = await client.put(
            WRITE, params={"path": "mount:docs/new.txt"}, json={"content": "hello"}
        )
    assert resp.status_code == 500
    assert resp.json()["detail"]  # names the underlying OS error, not a bare crash


async def test_mkdir_in_rw_mount_succeeds(tmp_path: Path) -> None:
    root, rw_spec = _rw_mount(tmp_path)
    client, _, _ = await _make_client(tmp_path, mount_specs=[rw_spec])
    async with client:
        resp = await client.post(DIR, params={"path": "mount:docs/newdir"})
    assert resp.status_code == 200
    assert resp.json()["path"] == "mount:docs/newdir"
    assert (root / "newdir").is_dir()


async def test_upload_into_rw_mount_succeeds(tmp_path: Path) -> None:
    root, rw_spec = _rw_mount(tmp_path)
    client, _, _ = await _make_client(tmp_path, mount_specs=[rw_spec])
    async with client:
        resp = await client.post(
            UPLOAD,
            params={"dir": "mount:docs"},
            files={"file": ("new.txt", b"hi", "text/plain")},
        )
    assert resp.status_code == 200
    assert resp.json()["path"] == "mount:docs/new.txt"
    assert (root / "new.txt").read_text(encoding="utf-8") == "hi"


async def test_delete_entry_from_rw_mount_succeeds(tmp_path: Path) -> None:
    root, rw_spec = _rw_mount(tmp_path)
    client, _, _ = await _make_client(tmp_path, mount_specs=[rw_spec])
    async with client:
        resp = await client.delete(ENTRY, params={"path": "mount:docs/report.md"})
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    assert not (root / "report.md").exists()


async def test_rename_within_rw_mount_succeeds(tmp_path: Path) -> None:
    root, rw_spec = _rw_mount(tmp_path)
    client, _, _ = await _make_client(tmp_path, mount_specs=[rw_spec])
    async with client:
        resp = await client.post(
            MOVE, json={"src": "mount:docs/report.md", "dst": "mount:docs/final.md"}
        )
    assert resp.status_code == 200
    assert resp.json()["path"] == "mount:docs/final.md"
    assert not (root / "report.md").exists()
    assert (root / "final.md").read_text(encoding="utf-8") == "draft"


# ── unknown mount name ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "url", "kwargs"),
    [
        ("get", LIST, {"params": {"path": "mount:ghost"}}),
        ("get", READ, {"params": {"path": "mount:ghost/x.txt"}}),
        ("get", STAT, {"params": {"path": "mount:ghost/x.txt"}}),
        ("get", DOWNLOAD, {"params": {"path": "mount:ghost/x.txt"}}),
    ],
)
async def test_unknown_mount_404s(
    tmp_path: Path, method: str, url: str, kwargs: dict[str, object]
) -> None:
    client, _, _ = await _make_client(tmp_path, mount_specs=[])
    async with client:
        resp = await getattr(client, method)(url, **kwargs)
    assert resp.status_code == 404


async def test_write_to_unknown_mount_404s(tmp_path: Path) -> None:
    client, _, _ = await _make_client(tmp_path, mount_specs=[])
    async with client:
        resp = await client.put(WRITE, params={"path": "mount:ghost/x.txt"}, json={"content": "x"})
    assert resp.status_code == 404


# ── traversal / symlink escape ───────────────────────────────────────────────────


async def test_dotdot_traversal_through_mount_prefix_rejected(tmp_path: Path) -> None:
    _, ro_spec = _ro_mount(tmp_path)
    client, _, _ = await _make_client(tmp_path, mount_specs=[ro_spec])
    async with client:
        resp = await client.get(READ, params={"path": "mount:media/../../../etc/passwd"})
    assert resp.status_code == 400


async def test_symlink_escape_from_mount_rejected(tmp_path: Path) -> None:
    root, ro_spec = _ro_mount(tmp_path)
    outside = tmp_path / "outside-secret"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope", encoding="utf-8")
    _symlink(root / "escape", outside)
    client, _, _ = await _make_client(tmp_path, mount_specs=[ro_spec])
    async with client:
        resp = await client.get(READ, params={"path": "mount:media/escape/secret.txt"})
    assert resp.status_code == 400


async def test_symlink_escape_via_list_rejected(tmp_path: Path) -> None:
    """list_dir has no local ValueError handler at all — proves the app-level net (#731)."""
    root, ro_spec = _ro_mount(tmp_path)
    outside = tmp_path / "outside-secret2"
    outside.mkdir()
    _symlink(root / "escape2", outside)
    client, _, _ = await _make_client(tmp_path, mount_specs=[ro_spec])
    async with client:
        resp = await client.get(LIST, params={"path": "mount:media/escape2"})
    assert resp.status_code == 400


# ── cross-store move is refused ───────────────────────────────────────────────────


async def test_move_from_tenant_into_mount_refused(tmp_path: Path) -> None:
    _, rw_spec = _rw_mount(tmp_path)
    client, _, _ = await _make_client(tmp_path, mount_specs=[rw_spec])
    async with client:
        resp = await client.post(MOVE, json={"src": "top.txt", "dst": "mount:docs/top.txt"})
    assert resp.status_code == 400


async def test_move_across_two_mounts_refused(tmp_path: Path) -> None:
    _, ro_spec = _ro_mount(tmp_path, "media")
    _, rw_spec = _rw_mount(tmp_path, "docs")
    client, _, _ = await _make_client(tmp_path, mount_specs=[ro_spec, rw_spec])
    async with client:
        resp = await client.post(
            MOVE, json={"src": "mount:docs/report.md", "dst": "mount:media/report.md"}
        )
    assert resp.status_code in (400, 403)  # RO dst refuses first either way


# ── indexed mounts: search + opt-in ──────────────────────────────────────────────


async def test_indexed_mount_is_searchable(tmp_path: Path) -> None:
    root = tmp_path / "kb"
    root.mkdir()
    (root / "roadmap.md").write_text("plans", encoding="utf-8")
    spec = MountSpec(name="kb", path=root, read_only=True, indexed=True, exclude=())
    client, _, _ = await _make_client(tmp_path, mount_specs=[spec])
    async with client:
        resp = await client.get(SEARCH, params={"q": "roadmap"})
    assert resp.status_code == 200
    paths = {e["path"] for e in resp.json()["entries"]}
    assert "mount:kb/roadmap.md" in paths


async def test_non_indexed_mount_is_not_searchable(tmp_path: Path) -> None:
    root = tmp_path / "kb2"
    root.mkdir()
    (root / "roadmap.md").write_text("plans", encoding="utf-8")
    spec = MountSpec(name="kb2", path=root, read_only=True, indexed=False, exclude=())
    client, _, _ = await _make_client(tmp_path, mount_specs=[spec])
    async with client:
        resp = await client.get(SEARCH, params={"q": "roadmap"})
    assert resp.json()["entries"] == []


async def test_indexed_mount_respects_exclude_globs(tmp_path: Path) -> None:
    root = tmp_path / "kb3"
    root.mkdir()
    (root / "keep.md").write_text("keep", encoding="utf-8")
    (root / "ignore.tmp").write_text("ignore", encoding="utf-8")
    spec = MountSpec(name="kb3", path=root, read_only=True, indexed=True, exclude=("*.tmp",))
    client, index, _ = await _make_client(tmp_path, mount_specs=[spec])
    async with client:
        pass
    hits = await index.search(tenant=TENANT, query="", limit=200)
    # search() requires a non-empty query in the route, but the index itself has no such
    # restriction — go straight to it to assert both rows' presence/absence directly.
    names = {h.name for h in hits if h.path.startswith("mount:kb3/")}
    assert names == {"keep.md"}
