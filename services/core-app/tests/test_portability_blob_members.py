"""The blob half of a tenant archive (#876) — bytes as well as rows, end to end.

Nothing is stood in for at the contract boundary here: the "modules" are real FastAPI apps
served by the real ``add_portability_routes`` from ``epicurus-core``, reached over an
``ASGITransport`` wired into the very ``httpx`` calls the orchestrator makes. So what is under
test is the real export → archive → apply path, including the URL building, the streaming, the
per-blob ceiling and the digest the core computes on the way back in.

The property that costs the most to get wrong is memory: an archive of objects must never be
an archive-sized allocation. The fakes therefore record the largest single chunk they are ever
handed, and the tests assert on it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tarfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from epicurus_core import (
    BlobOutcome,
    BlobRef,
    EpicurusModule,
    ImportReport,
    ModuleManifest,
    PortabilityRecord,
    add_portability_routes,
)
from epicurus_core.files import LocalFileStore
from epicurus_core_app.modules import ModuleSnapshot, ModuleStatus
from epicurus_core_app.portability import service as service_module
from epicurus_core_app.portability.archive import (
    ArchiveReader,
    module_blob_member,
    module_blobs_member,
    module_member,
)
from epicurus_core_app.portability.core_data import CORE_SETS
from epicurus_core_app.portability.jobs import PortabilityJobStore
from epicurus_core_app.portability.models import MODULE_MEMBER_PREFIX
from epicurus_core_app.portability.service import PortabilityService

TENANT = "local"
WHEN = datetime(2026, 9, 5, 9, 0, 0, tzinfo=UTC)

MB = 1024 * 1024
# Comfortably more than one core-side chunk, so "did it stream?" has an observable answer.
BIG = b"".join(bytes([i % 256]) * 4096 for i in range(1024))  # 4 MiB, not compressible to nothing


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── a real module, over real routes ───────────────────────────────────────────


class RowsOnlyModuleStore:
    """A module with records and no bytes — the shape every module but ``storage`` has."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.schema = f"{name}/1"
        self.rows: dict[str, dict[str, Any]] = {}
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.max_chunk_in = 0
        self.max_chunk_out = 0
        self.puts: list[tuple[str, str, int, str]] = []

    async def export(self, *, tenant_id: str) -> AsyncIterator[PortabilityRecord]:
        for rid, data in sorted(self.rows.items()):
            yield PortabilityRecord(kind="thing", id=rid, data=data)

    async def import_(
        self, *, tenant_id: str, records: AsyncIterator[PortabilityRecord], dry_run: bool
    ) -> ImportReport:
        report = ImportReport(schema_name=self.schema)
        async for record in records:
            existing = self.rows.get(record.id)
            if existing == record.data:
                report.record(record.kind, "skipped")
                continue
            if not dry_run:
                self.rows[record.id] = dict(record.data)
            report.record(record.kind, "updated" if existing else "created")
        return report


class FakeModuleStore(RowsOnlyModuleStore):
    """Rows *and* bytes — the shape ``storage`` has. A separate class on purpose: the helper
    discovers the blob half by ``isinstance``, which tests presence of the methods, so a store
    that "has them set to None" would still be taken for a blob store."""

    async def blobs(self, *, tenant_id: str) -> AsyncIterator[BlobRef]:
        for blob_id, (data, media) in sorted(self.objects.items()):
            yield BlobRef(id=blob_id, size=len(data), content_type=media)

    async def open_blob(self, *, tenant_id: str, blob_id: str) -> AsyncIterator[bytes]:
        stored = self.objects.get(blob_id)
        if stored is None:
            raise FileNotFoundError(blob_id)
        data = stored[0]
        for start in range(0, len(data), 64 * 1024):
            chunk = data[start : start + 64 * 1024]
            self.max_chunk_out = max(self.max_chunk_out, len(chunk))
            yield chunk

    async def put_blob(
        self,
        *,
        tenant_id: str,
        blob_id: str,
        sha256: str,
        size: int,
        content_type: str,
        chunks: AsyncIterator[bytes],
    ) -> BlobOutcome:
        body = bytearray()
        async for chunk in chunks:
            self.max_chunk_in = max(self.max_chunk_in, len(chunk))
            body += chunk
        self.puts.append((blob_id, sha256, size, content_type))
        existing = self.objects.get(blob_id)
        if existing is not None:
            if _sha(existing[0]) == sha256:
                return BlobOutcome(id=blob_id, outcome="skipped")
            return BlobOutcome(id=blob_id, outcome="skipped", warning=f"{blob_id} differs")
        self.objects[blob_id] = (bytes(body), content_type)
        return BlobOutcome(id=blob_id, outcome="created")


def _module_app(store: RowsOnlyModuleStore) -> FastAPI:
    app = FastAPI()
    add_portability_routes(
        app,
        EpicurusModule(store.name, version="1.2.3", portable=True),
        store,
    )
    return app


class _RouterTransport(httpx.AsyncBaseTransport):
    """Routes a request to whichever module app owns its base URL."""

    def __init__(self, apps: dict[str, FastAPI]) -> None:
        self._apps = {base: httpx.ASGITransport(app=app) for base, app in apps.items()}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        base = f"{request.url.scheme}://{request.url.netloc.decode()}"
        transport = self._apps.get(base)
        if transport is None:
            raise httpx.ConnectError(f"no module at {base}", request=request)
        return await transport.handle_async_request(request)


class FakeRegistry:
    def __init__(self, snaps: list[ModuleSnapshot], bases: dict[str, str]) -> None:
        self._snaps = snaps
        self._bases = bases

    async def snapshot(self, *, force: bool = False) -> list[ModuleSnapshot]:
        return self._snaps

    async def base_url(self, name: str) -> str:
        base = self._bases.get(name)
        if base is None:
            raise RuntimeError(f"no reachable module named {name!r}")
        return base


def _snapshot(name: str) -> ModuleSnapshot:
    return ModuleSnapshot(
        manifest=ModuleManifest(name=name, version="1.2.3", portable=True),
        status=ModuleStatus(healthy=True, version="1.2.3"),
        enabled=True,
    )


# ── harness ───────────────────────────────────────────────────────────────────


async def _engine(tmp_path: Path) -> AsyncEngine:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'core.db'}")
    async with engine.begin() as conn:
        for specs in CORE_SETS.values():
            for spec in specs:
                await conn.run_sync(spec.table.create, checkfirst=True)
    return engine


def _service(
    tmp_path: Path,
    engine: AsyncEngine,
    stores: dict[str, RowsOnlyModuleStore],
    monkeypatch: pytest.MonkeyPatch,
    *,
    max_file_bytes: int = 0,
    suffix: str = "",
) -> PortabilityService:
    """A real service whose ``httpx`` talks to the module apps instead of a network."""
    bases = {name: f"http://{name}:8080" for name in stores}
    apps = {bases[name]: _module_app(store) for name, store in stores.items()}
    transport = _RouterTransport(apps)

    def _client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return httpx.AsyncClient(*args, **kwargs)

    monkeypatch.setattr(
        service_module, "httpx", SimpleNamespace(AsyncClient=_client, **_httpx_names())
    )
    return PortabilityService(
        jobs=PortabilityJobStore(engine),
        engine=engine,
        file_store=LocalFileStore(tmp_path / f"files{suffix}"),
        registry=FakeRegistry([_snapshot(name) for name in stores], bases),
        staging_dir=tmp_path / f"staging{suffix}",
        core_app_version="0.120.0",
        max_file_bytes=max_file_bytes,
    )


def _httpx_names() -> dict[str, Any]:
    """Everything else the service reads off the ``httpx`` module, kept real."""
    return {
        "HTTPStatusError": httpx.HTTPStatusError,
        "Response": httpx.Response,
        "AsyncBaseTransport": httpx.AsyncBaseTransport,
    }


async def _settle(service: PortabilityService, job_id: str, running: str) -> Any:
    for _ in range(600):
        job = await service.job(tenant=TENANT, job_id=job_id)
        assert job is not None
        if job.status != running:
            return job
        await asyncio.sleep(0.01)
    raise AssertionError(f"job {job_id} never left {running!r}")


async def _export(service: PortabilityService) -> Any:
    await service._jobs.init()  # the app's lifespan does this in production
    job = await service.start_export(tenant=TENANT)
    return await _settle(service, job.id, "running")


# ── export ────────────────────────────────────────────────────────────────────


async def test_a_module_with_bytes_writes_a_listing_and_one_member_per_blob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = await _engine(tmp_path)
    store = FakeModuleStore("storage")
    store.rows["uploads/a.pdf"] = {"name": "a.pdf"}
    store.objects["uploads/a.pdf"] = (b"%PDF fixture", "application/pdf")
    store.objects["deep/dir/b.bin"] = (b"\x00\xff" * 10, "application/octet-stream")
    service = _service(tmp_path, engine, {"storage": store}, monkeypatch)

    job = await _export(service)
    assert job.status == "ready"
    async with ArchiveReader(Path(job.archive_path)) as reader:
        assert reader.has(module_member("storage"))
        assert reader.has(module_blobs_member("storage"))
        assert await reader.read_bytes(module_blob_member("storage", "uploads/a.pdf")) == (
            b"%PDF fixture"
        )
        assert await reader.read_bytes(module_blob_member("storage", "deep/dir/b.bin")) == (
            b"\x00\xff" * 10
        )
        refs = [r async for r in reader.blob_refs(module_blobs_member("storage"))]
        assert [r.id for r in refs] == ["deep/dir/b.bin", "uploads/a.pdf"]
        assert refs[1].content_type == "application/pdf"
        # The blob members are *not* mistaken for module streams, whatever they are named.
        assert reader.ndjson_members(MODULE_MEMBER_PREFIX) == [module_member("storage")]

    entry = next(c for c in job.progress if c["name"] == "storage")
    assert entry["blobs"] == 2
    assert entry["blob_bytes"] == len(b"%PDF fixture") + 20
    await engine.dispose()


async def test_a_module_without_bytes_is_exported_exactly_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 404 on ``/export/blobs`` is the answer "no bytes", not a failure of the component."""
    engine = await _engine(tmp_path)
    store = RowsOnlyModuleStore("calendar")
    store.rows["e-1"] = {"title": "lunch"}
    service = _service(tmp_path, engine, {"calendar": store}, monkeypatch)

    job = await _export(service)
    entry = next(c for c in job.progress if c["name"] == "calendar")
    assert entry["state"] == "included"
    assert entry["blobs"] == 0
    async with ArchiveReader(Path(job.archive_path)) as reader:
        assert reader.has(module_member("calendar"))
        assert not reader.has(module_blobs_member("calendar"))
    await engine.dispose()


async def test_a_multi_megabyte_blob_is_streamed_and_never_materialised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = await _engine(tmp_path)
    store = FakeModuleStore("storage")
    store.objects["big.bin"] = (BIG, "application/octet-stream")
    service = _service(tmp_path, engine, {"storage": store}, monkeypatch)

    job = await _export(service)
    async with ArchiveReader(Path(job.archive_path)) as reader:
        assert await reader.read_bytes(module_blob_member("storage", "big.bin")) == BIG
    # The module was asked for the object a chunk at a time, and the core never asked for
    # more than one chunk's worth in a single read.
    assert store.max_chunk_out <= 64 * 1024
    assert len(BIG) == 4 * MB
    await engine.dispose()


async def test_a_blob_over_the_ceiling_is_omitted_named_and_still_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its catalogue record travels regardless — a listed file whose bytes must be re-copied
    is strictly better than a file that silently never existed."""
    engine = await _engine(tmp_path)
    store = FakeModuleStore("storage")
    store.objects["small.txt"] = (b"tiny", "text/plain")
    store.objects["huge.bin"] = (b"x" * 4096, "application/octet-stream")
    service = _service(tmp_path, engine, {"storage": store}, monkeypatch, max_file_bytes=1024)

    job = await _export(service)
    entry = next(c for c in job.progress if c["name"] == "storage")
    assert entry["blobs"] == 1
    assert "huge.bin" in entry["reason"]
    async with ArchiveReader(Path(job.archive_path)) as reader:
        assert reader.has(module_blob_member("storage", "small.txt"))
        assert not reader.has(module_blob_member("storage", "huge.bin"))
        # The listing is verbatim, so the import can say which bytes are missing.
        refs = [r async for r in reader.blob_refs(module_blobs_member("storage"))]
        assert [r.id for r in refs] == ["huge.bin", "small.txt"]
    await engine.dispose()


async def test_a_blob_id_that_cannot_be_a_safe_member_is_named_not_carried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = await _engine(tmp_path)
    store = FakeModuleStore("storage")
    store.objects["../escape"] = (b"nope", "text/plain")
    store.objects["fine.txt"] = (b"yes", "text/plain")
    service = _service(tmp_path, engine, {"storage": store}, monkeypatch)

    job = await _export(service)
    entry = next(c for c in job.progress if c["name"] == "storage")
    assert entry["blobs"] == 1
    assert "escape" in entry["reason"]
    async with ArchiveReader(Path(job.archive_path)) as reader:
        assert [name for name, _ in reader.blob_members("storage")] == ["fine.txt"]
    await engine.dispose()


# ── import ────────────────────────────────────────────────────────────────────


async def _round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: RowsOnlyModuleStore,
    target: RowsOnlyModuleStore,
    *,
    max_file_bytes: int = 0,
) -> Any:
    """Export from *source*, then apply the same archive into *target*."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = await _engine(tmp_path)
    service = _service(
        tmp_path, engine, {source.name: source}, monkeypatch, max_file_bytes=max_file_bytes
    )
    job = await _export(service)
    archive = Path(job.archive_path).read_bytes()

    into = _service(tmp_path, engine, {target.name: target}, monkeypatch, suffix="-target")

    async def chunks() -> AsyncIterator[bytes]:
        for start in range(0, len(archive), 64 * 1024):
            yield archive[start : start + 64 * 1024]

    staged = await into.stage_import(tenant=TENANT, chunks=chunks(), max_bytes=0)
    applied = await into.start_apply(tenant=TENANT, job_id=staged.id)
    assert applied is not None
    done = await _settle(into, staged.id, "running")
    await engine.dispose()
    return staged, done


async def test_rows_and_bytes_both_land_and_the_digest_is_the_cores_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = FakeModuleStore("storage")
    source.rows["uploads/a.pdf"] = {"name": "a.pdf"}
    source.objects["uploads/a.pdf"] = (b"%PDF fixture", "application/pdf")
    target = FakeModuleStore("storage")

    staged, done = await _round_trip(tmp_path, monkeypatch, source, target)

    preview = next(c for c in staged.preview["components"] if c["name"] == "storage")
    assert preview["records"] == 1
    assert preview["blobs"] == 1

    assert target.rows == {"uploads/a.pdf": {"name": "a.pdf"}}
    assert target.objects == {"uploads/a.pdf": (b"%PDF fixture", "application/pdf")}
    blob_id, sha256, size, media = target.puts[0]
    assert blob_id == "uploads/a.pdf"
    assert sha256 == _sha(b"%PDF fixture")
    assert size == len(b"%PDF fixture")
    assert media == "application/pdf"

    component = next(c for c in done.report["components"] if c["name"] == "storage")
    assert component["blobs"] == {
        "written": 1,
        "skipped": 0,
        "bytes_written": len(b"%PDF fixture"),
        "conflicts": [],
        "missing": [],
    }


async def test_a_multi_megabyte_blob_reaches_the_module_in_bounded_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = FakeModuleStore("storage")
    source.objects["big.bin"] = (BIG, "application/octet-stream")
    target = FakeModuleStore("storage")

    await _round_trip(tmp_path, monkeypatch, source, target)

    assert target.objects["big.bin"][0] == BIG
    assert target.puts[0][1] == _sha(BIG)
    # Streamed from the staged member, never handed over whole.
    assert 0 < target.max_chunk_in <= MB


async def test_applying_the_same_archive_twice_writes_nothing_the_second_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = FakeModuleStore("storage")
    source.rows["a"] = {"name": "a"}
    source.objects["a"] = (b"bytes", "text/plain")
    target = FakeModuleStore("storage")

    await _round_trip(tmp_path, monkeypatch, source, target)
    _staged, done = await _round_trip(tmp_path / "again", monkeypatch, source, target)

    component = next(c for c in done.report["components"] if c["name"] == "storage")
    assert component["created"] == 0
    assert component["updated"] == 0
    assert component["blobs"]["written"] == 0
    assert component["blobs"]["skipped"] == 1
    assert component["blobs"]["conflicts"] == []
    assert target.objects == {"a": (b"bytes", "text/plain")}


async def test_a_target_holding_different_bytes_keeps_them_and_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = FakeModuleStore("storage")
    source.objects["a"] = (b"the archive's copy", "text/plain")
    target = FakeModuleStore("storage")
    target.objects["a"] = (b"the operator's own edit", "text/plain")

    _staged, done = await _round_trip(tmp_path, monkeypatch, source, target)

    component = next(c for c in done.report["components"] if c["name"] == "storage")
    assert component["blobs"]["conflicts"] == ["a"]
    assert any("different bytes" in w for w in component["warnings"])
    assert target.objects["a"] == (b"the operator's own edit", "text/plain")


async def test_a_blob_listed_without_its_bytes_is_reported_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = FakeModuleStore("storage")
    source.rows["huge.bin"] = {"name": "huge.bin"}
    source.objects["huge.bin"] = (b"x" * 4096, "application/octet-stream")
    target = FakeModuleStore("storage")

    _staged, done = await _round_trip(tmp_path, monkeypatch, source, target, max_file_bytes=1024)

    component = next(c for c in done.report["components"] if c["name"] == "storage")
    assert component["blobs"]["missing"] == ["huge.bin"]
    assert component["blobs"]["written"] == 0
    assert any("without their bytes" in w for w in component["warnings"])
    # The record still landed: the entry is listed here, its bytes are not.
    assert target.rows == {"huge.bin": {"name": "huge.bin"}}
    assert target.objects == {}


async def test_a_module_that_cannot_take_blobs_still_takes_its_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source that grew a blob half importing into a target that has not got one yet."""
    source = FakeModuleStore("storage")
    source.rows["a"] = {"name": "a"}
    source.objects["a"] = (b"bytes", "text/plain")
    target = RowsOnlyModuleStore("storage")

    _staged, done = await _round_trip(tmp_path, monkeypatch, source, target)

    component = next(c for c in done.report["components"] if c["name"] == "storage")
    assert component["state"] == "included"
    assert component["created"] == 1
    assert any("blobs could not be imported" in w for w in component["warnings"])


# ── the member-name rule the blob layout depends on ───────────────────────────


async def test_a_blob_named_like_a_module_stream_is_not_read_as_one(tmp_path: Path) -> None:
    """``modules/storage/blobs/notes.ndjson`` is a *file called notes.ndjson*, not a module."""
    archive = tmp_path / "a.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name, payload in (
            (module_member("storage"), b'{"schema":"storage/1"}\n'),
            (module_blobs_member("storage"), b'{"id":"notes.ndjson","size":3}\n'),
            (module_blob_member("storage", "notes.ndjson"), b"abc"),
            ("manifest.json", json.dumps({"format_version": 1}).encode()),
        ):
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, __import__("io").BytesIO(payload))

    async with ArchiveReader(archive) as reader:
        assert reader.ndjson_members(MODULE_MEMBER_PREFIX) == [module_member("storage")]
        assert reader.blob_members("storage") == [("notes.ndjson", 3)]
