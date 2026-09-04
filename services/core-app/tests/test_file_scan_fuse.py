"""The file-scan mass de-index fuse (#848) — an empty tree must not purge the index.

Drives the real :func:`scan` over a real :class:`LocalFileStore`, so the "stale mount" case
is reproduced the way it happened: the tenant root exists and is empty, while the index
still holds every row. The unit tests at the top pin the policy's edges; the rest assert
what the scan actually does to ``core_files``.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from epicurus_core import route_paths
from epicurus_core.files import LocalFileStore
from epicurus_core_app.file_index import FileIndex
from epicurus_core_app.file_scan import ScanFuse, namespace_for, scan
from epicurus_core_app.files_routes import create_files_router

TENANT = "local"
OTHER = "other"


async def _fresh_index(tmp_path: Path) -> tuple[FileIndex, AsyncEngine]:
    """A file-backed SQLite index — never in-memory + StaticPool (see AGENTS.md)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'index.db'}")
    index = FileIndex(engine)
    await index.init()
    return index, engine


def _tree(root: Path, tenant: str = TENANT, files: int = 10) -> None:
    tenant_root = root / tenant
    (tenant_root / "notes").mkdir(parents=True)
    for n in range(files):
        (tenant_root / "notes" / f"file_{n}.txt").write_text(str(n), encoding="utf-8")


def _empty_the_mount(root: Path, tenant: str = TENANT) -> None:
    """What the container saw on 2026-08-30: the tenant root is there, its contents are not.

    Deliberately leaves the root itself in place — an *absent* root is the case the store
    already handled; the incident's signature is a root that exists and lists nothing.
    """
    for path in sorted((root / tenant).rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()


# ── The policy in isolation ───────────────────────────────────────────────────


def test_first_scan_never_trips() -> None:
    fuse = ScanFuse()
    assert (
        fuse.evaluate(
            tenant=TENANT, namespace="tenant", indexed_rows=0, would_delete=0, seen_entries=0
        )
        is None
    )


def test_empty_store_with_a_populated_index_always_trips() -> None:
    trip = ScanFuse().evaluate(
        tenant=TENANT, namespace="tenant", indexed_rows=1, would_delete=1, seen_entries=0
    )
    assert trip is not None
    assert "read empty" in trip.reason  # below the floor, but total disappearance


def test_purge_above_the_ratio_trips_and_below_does_not() -> None:
    fuse = ScanFuse()
    assert (
        fuse.evaluate(
            tenant=TENANT, namespace="tenant", indexed_rows=20, would_delete=10, seen_entries=10
        )
        is not None
    )
    assert (
        fuse.evaluate(
            tenant=TENANT, namespace="tenant", indexed_rows=20, would_delete=9, seen_entries=11
        )
        is None
    )


def test_purge_below_the_floor_does_not_trip() -> None:
    assert (
        ScanFuse().evaluate(
            tenant=TENANT, namespace="tenant", indexed_rows=4, would_delete=3, seen_entries=1
        )
        is None
    )


def test_force_and_disabled_both_bypass() -> None:
    args = {"tenant": TENANT, "namespace": "tenant", "indexed_rows": 9, "would_delete": 9}
    assert ScanFuse().evaluate(**args, seen_entries=0, force=True) is None  # type: ignore[arg-type]
    assert ScanFuse(enabled=False).evaluate(**args, seen_entries=0) is None  # type: ignore[arg-type]


def test_namespace_labels_are_shared_by_scan_and_api() -> None:
    assert namespace_for("") == "tenant"
    assert namespace_for("mount:photos/") == "mount:photos/"


# ── The scan, over a real store ───────────────────────────────────────────────


async def test_scan_refuses_to_purge_an_empty_tree(tmp_path: Path) -> None:
    _tree(tmp_path)
    store = LocalFileStore(tmp_path)
    index, engine = await _fresh_index(tmp_path)
    fuse = ScanFuse()
    try:
        await scan(store, index, tenant=TENANT, fuse=fuse)
        assert await index.count_rows(tenant=TENANT) == 11  # 10 files + the notes dir

        _empty_the_mount(tmp_path)
        total = await scan(store, index, tenant=TENANT, fuse=fuse)

        assert total == 0
        assert await index.count_rows(tenant=TENANT) == 11  # nothing was purged
        assert fuse.tripped is True
        trip = fuse.trips()[0]
        assert (trip.tenant, trip.namespace, trip.would_delete) == (TENANT, "tenant", 11)
    finally:
        await engine.dispose()


async def test_scan_refuses_a_purge_above_the_threshold(tmp_path: Path) -> None:
    _tree(tmp_path)
    store = LocalFileStore(tmp_path)
    index, engine = await _fresh_index(tmp_path)
    fuse = ScanFuse()
    try:
        await scan(store, index, tenant=TENANT, fuse=fuse)
        for n in range(8):
            (tmp_path / TENANT / "notes" / f"file_{n}.txt").unlink()

        await scan(store, index, tenant=TENANT, fuse=fuse)

        assert fuse.tripped is True
        assert await index.count_rows(tenant=TENANT) == 11
    finally:
        await engine.dispose()


async def test_scan_still_purges_an_ordinary_deletion(tmp_path: Path) -> None:
    _tree(tmp_path)
    store = LocalFileStore(tmp_path)
    index, engine = await _fresh_index(tmp_path)
    fuse = ScanFuse()
    try:
        await scan(store, index, tenant=TENANT, fuse=fuse)
        (tmp_path / TENANT / "notes" / "file_0.txt").unlink()

        await scan(store, index, tenant=TENANT, fuse=fuse)

        assert fuse.tripped is False
        assert await index.count_rows(tenant=TENANT) == 10
        assert await index.get(tenant=TENANT, path="notes/file_0.txt") is None
    finally:
        await engine.dispose()


async def test_first_scan_of_an_empty_root_does_not_trip(tmp_path: Path) -> None:
    (tmp_path / TENANT).mkdir()
    store = LocalFileStore(tmp_path)
    index, engine = await _fresh_index(tmp_path)
    fuse = ScanFuse()
    try:
        assert await scan(store, index, tenant=TENANT, fuse=fuse) == 0
        assert fuse.tripped is False
    finally:
        await engine.dispose()


async def test_force_purges_what_the_fuse_refused(tmp_path: Path) -> None:
    _tree(tmp_path)
    store = LocalFileStore(tmp_path)
    index, engine = await _fresh_index(tmp_path)
    fuse = ScanFuse()
    try:
        await scan(store, index, tenant=TENANT, fuse=fuse)
        _empty_the_mount(tmp_path)
        await scan(store, index, tenant=TENANT, fuse=fuse)
        assert fuse.tripped is True

        await scan(store, index, tenant=TENANT, fuse=fuse, force=True)

        assert await index.count_rows(tenant=TENANT) == 0
        assert fuse.tripped is False  # a completed purge re-arms it
    finally:
        await engine.dispose()


async def test_a_repaired_mount_re_arms_the_fuse(tmp_path: Path) -> None:
    _tree(tmp_path)
    store = LocalFileStore(tmp_path)
    index, engine = await _fresh_index(tmp_path)
    fuse = ScanFuse()
    try:
        await scan(store, index, tenant=TENANT, fuse=fuse)
        _empty_the_mount(tmp_path)
        await scan(store, index, tenant=TENANT, fuse=fuse)
        assert fuse.tripped is True

        _tree(tmp_path)  # the mount comes back
        await scan(store, index, tenant=TENANT, fuse=fuse)

        assert fuse.tripped is False
        assert fuse.trips() == []
        assert await index.count_rows(tenant=TENANT) == 11
    finally:
        await engine.dispose()


async def test_scanning_without_a_fuse_behaves_as_before(tmp_path: Path) -> None:
    _tree(tmp_path)
    store = LocalFileStore(tmp_path)
    index, engine = await _fresh_index(tmp_path)
    try:
        await scan(store, index, tenant=TENANT)
        _empty_the_mount(tmp_path)

        await scan(store, index, tenant=TENANT)

        assert await index.count_rows(tenant=TENANT) == 0
    finally:
        await engine.dispose()


async def test_fuse_is_tenant_and_namespace_scoped(tmp_path: Path) -> None:
    """A suspect tenant tree must not withhold another tenant's purge, or a mount's."""
    _tree(tmp_path)
    _tree(tmp_path, tenant=OTHER)
    store = LocalFileStore(tmp_path)
    index, engine = await _fresh_index(tmp_path)
    fuse = ScanFuse()
    try:
        await scan(store, index, tenant=TENANT, fuse=fuse)
        await scan(store, index, tenant=OTHER, fuse=fuse)

        _empty_the_mount(tmp_path)
        await scan(store, index, tenant=TENANT, fuse=fuse)

        assert [t.tenant for t in fuse.trips()] == [TENANT]
        # The other tenant's rows were never in scope: its own scan still converges.
        (tmp_path / OTHER / "notes" / "file_0.txt").unlink()
        await scan(store, index, tenant=OTHER, fuse=fuse)
        assert await index.count_rows(tenant=OTHER) == 10
        assert await index.count_rows(tenant=TENANT) == 11
    finally:
        await engine.dispose()


async def test_mount_namespace_purge_is_weighed_against_the_mount_alone(
    tmp_path: Path,
) -> None:
    """A mount's rows share the tenant's table; the fuse must weigh only that namespace."""
    _tree(tmp_path)
    mount_root = tmp_path / "external"
    _tree(mount_root, files=2)
    store = LocalFileStore(tmp_path)
    mount_store = LocalFileStore(mount_root)
    index, engine = await _fresh_index(tmp_path)
    fuse = ScanFuse()
    prefix = "mount:photos/"
    try:
        await scan(store, index, tenant=TENANT, fuse=fuse)
        await scan(mount_store, index, tenant=TENANT, path_prefix=prefix, fuse=fuse)
        assert await index.count_rows(tenant=TENANT, path_prefix=prefix) == 3

        _empty_the_mount(mount_root)
        await scan(mount_store, index, tenant=TENANT, path_prefix=prefix, fuse=fuse)

        # Only the mount refuses — it is weighed against its own 3 rows, not the tenant's 14 —
        # and its rows survive; the healthy tenant tree's fuse is untouched by its neighbour.
        assert [t.namespace for t in fuse.trips()] == [prefix]
        assert fuse.trips()[0].indexed_rows == 3
        assert await index.count_rows(tenant=TENANT, path_prefix=prefix) == 3
    finally:
        await engine.dispose()


# ── The index counters the fuse is built on ───────────────────────────────────


async def test_count_stale_matches_what_purge_would_delete(tmp_path: Path) -> None:
    _tree(tmp_path, files=4)
    store = LocalFileStore(tmp_path)
    index, engine = await _fresh_index(tmp_path)
    try:
        await scan(store, index, tenant=TENANT)
        seen = {"notes", "notes/file_0.txt"}

        predicted = await index.count_stale(tenant=TENANT, seen_paths=seen)
        purged = await index.purge_stale(tenant=TENANT, seen_paths=seen)

        assert predicted == purged == 3
    finally:
        await engine.dispose()


async def test_count_stale_of_nothing_seen_is_the_whole_namespace(tmp_path: Path) -> None:
    _tree(tmp_path, files=4)
    store = LocalFileStore(tmp_path)
    index, engine = await _fresh_index(tmp_path)
    try:
        await scan(store, index, tenant=TENANT)
        assert await index.count_stale(tenant=TENANT, seen_paths=set()) == 5
        assert await index.count_stale(tenant=OTHER, seen_paths=set()) == 0
    finally:
        await engine.dispose()


# ── The operator surface: GET /scan-status, POST /rescan ──────────────────────


async def _client(tmp_path: Path, fuse: ScanFuse) -> tuple[AsyncClient, FileIndex, AsyncEngine]:
    """The files router wired the way app.py wires it, over a real store and index."""
    _tree(tmp_path)
    store = LocalFileStore(tmp_path)
    index, engine = await _fresh_index(tmp_path)

    async def _rescan(namespace: str = "", force: bool = False, tenant: str | None = None) -> int:
        if namespace:
            raise KeyError(namespace)  # no mounts in this fixture
        return await scan(store, index, tenant=tenant or TENANT, fuse=fuse, force=force)

    await _rescan()
    app = FastAPI()
    app.include_router(
        create_files_router(
            store, default_tenant=TENANT, index=index, scan_fuse=fuse, rescan=_rescan
        )
    )
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    return client, index, engine


async def test_scan_status_reports_an_armed_fuse(tmp_path: Path) -> None:
    fuse = ScanFuse()
    client, _, engine = await _client(tmp_path, fuse)
    try:
        body = (await client.get("/platform/v1/files/scan-status")).json()
        assert body["fuse_enabled"] is True
        assert body["tripped"] is False
        assert body["namespaces"] == []
        assert body["max_delete_ratio"] == 0.5
    finally:
        await client.aclose()
        await engine.dispose()


async def test_scan_status_reports_a_tripped_fuse(tmp_path: Path) -> None:
    fuse = ScanFuse()
    client, index, engine = await _client(tmp_path, fuse)
    try:
        _empty_the_mount(tmp_path)
        rescan = (await client.post("/platform/v1/files/rescan")).json()
        assert rescan == {"namespace": "", "entries": 0, "forced": False, "tripped": True}

        body = (await client.get("/platform/v1/files/scan-status")).json()
        assert body["tripped"] is True
        [ns] = body["namespaces"]
        assert ns["tenant"] == TENANT
        assert ns["namespace"] == "tenant"
        assert ns["would_delete"] == 11
        assert "read empty" in ns["reason"]
        assert await index.count_rows(tenant=TENANT) == 11  # the rows are still there
    finally:
        await client.aclose()
        await engine.dispose()


async def test_forced_rescan_purges_and_re_arms(tmp_path: Path) -> None:
    fuse = ScanFuse()
    client, index, engine = await _client(tmp_path, fuse)
    try:
        _empty_the_mount(tmp_path)
        await client.post("/platform/v1/files/rescan")

        forced = (await client.post("/platform/v1/files/rescan", params={"force": True})).json()

        assert forced == {"namespace": "", "entries": 0, "forced": True, "tripped": False}
        assert await index.count_rows(tenant=TENANT) == 0
        assert (await client.get("/platform/v1/files/scan-status")).json()["tripped"] is False
    finally:
        await client.aclose()
        await engine.dispose()


async def test_rescan_of_an_unknown_mount_is_a_404(tmp_path: Path) -> None:
    fuse = ScanFuse()
    client, _, engine = await _client(tmp_path, fuse)
    try:
        response = await client.post("/platform/v1/files/rescan", params={"namespace": "nope"})
        assert response.status_code == 404
        assert "not an indexed mount" in response.json()["detail"]
    finally:
        await client.aclose()
        await engine.dispose()


async def test_routes_are_absent_without_the_fuse_wiring(tmp_path: Path) -> None:
    """The pair is opt-in: a router built without them is byte-for-byte the old surface."""
    _tree(tmp_path)
    index, engine = await _fresh_index(tmp_path)
    try:
        app = FastAPI()
        app.include_router(
            create_files_router(LocalFileStore(tmp_path), default_tenant=TENANT, index=index)
        )
        paths = route_paths(app)
        assert "/platform/v1/files/scan-status" not in paths
        assert "/platform/v1/files/rescan" not in paths
    finally:
        await engine.dispose()


async def test_scan_status_is_scoped_to_the_tenant_asked_about(tmp_path: Path) -> None:
    """Constraint #1: the fuse keys its state per tenant, and so must the read of it.

    ``ScanFuse`` holds every tenant's trips in one process-wide dict, so an unscoped
    ``/scan-status`` would hand one tenant another's namespace names and row counts — inert
    while v1 is single-tenant, and invisible right up until it is not.
    """
    fuse = ScanFuse()
    client, _, engine = await _client(tmp_path, fuse)
    try:
        _empty_the_mount(tmp_path)
        await client.post("/platform/v1/files/rescan")  # trips for TENANT

        ours = (await client.get("/platform/v1/files/scan-status")).json()
        assert ours["tripped"] is True
        assert [n["tenant"] for n in ours["namespaces"]] == [TENANT]

        theirs = (
            await client.get("/platform/v1/files/scan-status", params={"tenant_id": "someone-else"})
        ).json()
        assert theirs["tripped"] is False
        assert theirs["namespaces"] == []
        # The thresholds are policy, not tenant data — those stay visible either way.
        assert theirs["max_delete_ratio"] == ours["max_delete_ratio"]
    finally:
        await client.aclose()
        await engine.dispose()


async def test_rescan_scopes_its_scan_and_its_verdict_to_the_tenant(tmp_path: Path) -> None:
    """The door carries the tenant through to the scan and back out of the fuse read."""
    fuse = ScanFuse()
    client, index, engine = await _client(tmp_path, fuse)
    try:
        _empty_the_mount(tmp_path)
        mine = (await client.post("/platform/v1/files/rescan")).json()
        assert mine["tripped"] is True
        assert await index.count_rows(tenant=TENANT) > 0  # withheld, not purged

        # A different tenant has nothing indexed, so nothing to protect and nothing to trip —
        # and it must not inherit this tenant's verdict.
        other = (
            await client.post("/platform/v1/files/rescan", params={"tenant_id": "someone-else"})
        ).json()
        assert other["tripped"] is False
        assert await index.count_rows(tenant="someone-else") == 0
    finally:
        await client.aclose()
        await engine.dispose()
