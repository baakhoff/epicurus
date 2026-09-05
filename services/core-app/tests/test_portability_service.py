"""The tenant export/import orchestrator (#867), end to end over a real archive.

Real everything that can be real: a file-backed SQLite database, a real
:class:`~epicurus_core.files.LocalFileStore` tree, a real ``.tar.gz`` written to a real
staging directory, read back by the real reader. Only the two things that would need a
network are stood in for — the module fleet and the fact store — and they are replaced at
the same seams the production code uses (``_stream_module_export`` /
``_post_module_import`` / ``_module_schema``, exactly as ``ModuleRegistry._post_reindex`` is
overridden in the registry's own tests).
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import tarfile
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from epicurus_core import ImportReport, ModuleManifest
from epicurus_core.files import FileStore, LocalFileStore
from epicurus_core_app.modules import ModuleSnapshot, ModuleStatus
from epicurus_core_app.portability.archive import ArchiveReader, sanitize_member
from epicurus_core_app.portability.core_data import CORE_SETS
from epicurus_core_app.portability.jobs import PortabilityJobStore
from epicurus_core_app.portability.models import (
    PORTABILITY_FORMAT_VERSION,
    ArchiveManifest,
    ImportPreview,
    ImportReportView,
    SecretsInventory,
)
from epicurus_core_app.portability.secrets import collect_module_secrets
from epicurus_core_app.portability.service import PortabilityService

TENANT = "local"
WHEN = datetime(2026, 9, 4, 9, 0, 0, tzinfo=UTC)

# The fixture's *secret material*. It is handed to the service the only way a secret ever
# reaches it — as an inventory of names — and must never appear in the archive's bytes.
PROVIDER_KEY_VALUE = "sk-fixture-3f9a-DO-NOT-EXPORT"
OAUTH_TOKEN_VALUE = "ya29.fixture-refresh-DO-NOT-EXPORT"
BRIDGE_TOKEN_VALUE = "bot-fixture-discord-DO-NOT-EXPORT"


# ── stand-ins ─────────────────────────────────────────────────────────────────


@dataclass
class FakeFact:
    id: str
    text: str
    source: str = "auto"
    created_at: datetime | None = None


class FakeFacts:
    """The memory store's read/write surface, with ``save``'s real dedup semantics."""

    def __init__(self, texts: Sequence[str] = ()) -> None:
        self.facts = [FakeFact(id=f"f-{i}", text=t, created_at=WHEN) for i, t in enumerate(texts)]

    async def list_facts(self, *, tenant: str, limit: int = 200, cap: int = 2000) -> list[FakeFact]:
        return list(self.facts[:limit])

    async def save(self, *, tenant: str, text: str, source: str = "auto") -> FakeFact | None:
        if any(f.text == text for f in self.facts):
            return None  # the real store's embedding dedup, in miniature
        fact = FakeFact(id=f"f-{len(self.facts)}", text=text, source=source, created_at=WHEN)
        self.facts.append(fact)
        return fact


class FakeRegistry:
    """A module fleet: name -> (manifest, healthy, enabled, base)."""

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


def _snapshot(
    name: str,
    *,
    portable: bool = True,
    healthy: bool = True,
    enabled: bool = True,
    removed: bool = False,
    secrets: list[str] | None = None,
) -> ModuleSnapshot:
    return ModuleSnapshot(
        manifest=ModuleManifest(
            name=name, version="1.2.3", portable=portable, secrets=secrets or []
        ),
        status=ModuleStatus(healthy=healthy, version="1.2.3"),
        enabled=enabled,
        removed=removed,
    )


def _module_stream(schema: str, records: list[dict[str, Any]]) -> bytes:
    header = json.dumps({"schema": schema, "component_version": "1.2.3"})
    return "\n".join([header, *(json.dumps(r) for r in records)]).encode() + b"\n"


@dataclass
class ModuleCalls:
    exports: list[str] = field(default_factory=list)
    imports: dict[str, bytes] = field(default_factory=dict)


class TestService(PortabilityService):
    """The real service with its three module HTTP calls replaced by in-memory streams."""

    __test__ = False  # not a pytest test class

    def __init__(self, *, streams: dict[str, bytes], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.streams = streams
        self.calls = ModuleCalls()
        self.import_failures: dict[str, int] = {}

    async def _module_schema(self, base: str, tenant: str) -> str | None:
        body = self.streams.get(base)
        if body is None:
            return None
        return str(json.loads(body.splitlines()[0])["schema"])

    async def _stream_module_export(self, base: str, tenant: str, target: Path) -> int:
        self.calls.exports.append(base)
        body = self.streams[base]
        target.write_bytes(body)
        return max(body.count(b"\n") - 1, 0)

    async def _post_module_import(
        self, base: str, tenant: str, body: AsyncIterator[bytes]
    ) -> ImportReport:
        received = b""
        async for chunk in body:
            received += chunk
        self.calls.imports[base] = received
        lines = [line for line in received.splitlines() if line.strip()]
        report = ImportReport(schema_name=str(json.loads(lines[0])["schema"]))
        for line in lines[1:]:
            report.record(str(json.loads(line)["kind"]), "created")
        return report


# ── fixtures ──────────────────────────────────────────────────────────────────


async def _engine(tmp_path: Path) -> AsyncEngine:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'core.db'}")
    async with engine.begin() as conn:
        for specs in CORE_SETS.values():
            for spec in specs:
                await conn.run_sync(spec.table.create, checkfirst=True)
    return engine


async def _seed_core(engine: AsyncEngine) -> None:
    specs = {spec.kind: spec for group in CORE_SETS.values() for spec in group}
    async with engine.begin() as conn:
        await conn.execute(
            insert(specs["agent_messages"].table).values(
                tenant=TENANT,
                session_id="s-1",
                role="user",
                content="hello",
                created_at=WHEN,
            )
        )
        await conn.execute(
            insert(specs["timezone_prefs"].table).values(tenant=TENANT, timezone="Europe/Berlin")
        )


async def _secrets(_tenant: str) -> SecretsInventory:
    """Names only — the values above are deliberately not passed anywhere near the archive."""
    return SecretsInventory(
        provider_keys=["openai"],
        connected_accounts=["google"],
        module_secrets={"messaging": ["messaging/discord", "messaging/telegram"]},
    )


def _service(
    tmp_path: Path,
    engine: AsyncEngine,
    *,
    snaps: list[ModuleSnapshot] | None = None,
    bases: dict[str, str] | None = None,
    streams: dict[str, bytes] | None = None,
    facts: FakeFacts | None = None,
    rescans: list[tuple[bool, str | None]] | None = None,
    reembeds: list[dict[str, str]] | None = None,
    max_file_bytes: int = 0,
) -> TestService:
    async def rescan(force: bool = False, tenant: str | None = None) -> int:
        # Records the *tenant* as well as the force flag: the real helper defaults to the
        # deployment tenant when handed none, so a dropped tenant here would rebuild the
        # wrong tree's index and never fail a test that only looked at ``force``.
        if rescans is not None:
            rescans.append((force, tenant))
        return 7

    async def reembed() -> list[dict[str, str]]:
        return reembeds if reembeds is not None else [{"module": "knowledge", "status": "started"}]

    return TestService(
        streams=streams or {},
        jobs=PortabilityJobStore(engine),
        engine=engine,
        file_store=LocalFileStore(tmp_path / "files"),
        registry=FakeRegistry(snaps or [], bases or {}),
        staging_dir=tmp_path / "staging",
        core_app_version="0.118.0",
        facts=facts,
        secrets_inventory=_secrets,
        rescan=rescan,
        reembed=reembed,
        max_file_bytes=max_file_bytes,
    )


async def _settle(service: PortabilityService, tenant: str, job_id: str, running: str) -> Any:
    """Poll the job until it leaves *running*, the way the shell does."""
    for _ in range(600):
        job = await service.job(tenant=tenant, job_id=job_id)
        assert job is not None
        if job.status != running:
            return job
        await asyncio.sleep(0.01)
    raise AssertionError(f"job {job_id} never left {running!r}")


async def _write_file(store: FileStore, path: str, data: bytes) -> None:
    await store.write_bytes(tenant=TENANT, path=path, data=data)


# ── export ────────────────────────────────────────────────────────────────────


async def test_export_writes_a_readable_archive_with_every_component(tmp_path: Path) -> None:
    engine = await _engine(tmp_path)
    await _seed_core(engine)
    service = _service(
        tmp_path,
        engine,
        snaps=[_snapshot("calendar")],
        bases={"calendar": "http://calendar:8080"},
        streams={
            "http://calendar:8080": _module_stream(
                "calendar/1", [{"kind": "event", "id": "e-1", "data": {"title": "lunch"}}]
            )
        },
        facts=FakeFacts(["the user prefers mornings"]),
    )
    await service._jobs.init()
    await service._files.ensure_tenant_root(tenant=TENANT)
    await _write_file(service._files, "notes/hello.md", b"# hello")

    try:
        started = await service.start_export(tenant=TENANT)
        job = await _settle(service, TENANT, started.id, "running")
        assert job.status == "ready", job.error
        manifest = ArchiveManifest.model_validate(job.manifest)

        async with ArchiveReader(Path(job.archive_path or "")) as reader:
            written = await reader.manifest()
            assert reader.has("core/conversations.ndjson")
            assert reader.has("core/prefs.ndjson")
            assert reader.has("core/memory.ndjson")
            assert reader.has("modules/calendar.ndjson")
            assert reader.file_members() == [("notes/hello.md", 7)]
            assert await reader.count_records("modules/calendar.ndjson") == 1
            assert await reader.count_records("core/memory.ndjson") == 1
            conversations = [r async for r in reader.records("core/conversations.ndjson")]
            assert [r.kind for r in conversations] == ["agent_messages"]
    finally:
        await engine.dispose()

    assert written.format_version == PORTABILITY_FORMAT_VERSION
    assert written.tenant == TENANT
    assert written.core_app_version == "0.118.0"
    states = {c.name: c for c in manifest.components}
    assert states["calendar"].state == "included"
    assert states["calendar"].count == 1
    assert states["files"].count == 1
    assert states["conversations"].schema_name == "core/1"
    # The exclusions are in the archive, not just in the docs.
    assert any("core_files" in e.component for e in written.exclusions)
    assert any("secrets" in e.component for e in written.exclusions)
    assert written.secrets.provider_keys == ["openai"]
    assert written.secrets.connected_accounts == ["google"]


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        (_snapshot("storage", portable=False), "does not declare portable data"),
        (_snapshot("mail", healthy=False), "unreachable"),
        (_snapshot("tasks", enabled=False), "disabled"),
    ],
)
async def test_a_module_that_cannot_be_asked_is_skipped_not_fatal(
    tmp_path: Path, snapshot: ModuleSnapshot, expected: str
) -> None:
    engine = await _engine(tmp_path)
    await _seed_core(engine)
    service = _service(tmp_path, engine, snaps=[snapshot])
    await service._jobs.init()
    await service._files.ensure_tenant_root(tenant=TENANT)

    try:
        started = await service.start_export(tenant=TENANT)
        job = await _settle(service, TENANT, started.id, "running")
    finally:
        await engine.dispose()

    assert job.status == "ready", job.error
    entry = next(
        c for c in ArchiveManifest.model_validate(job.manifest).components if c.kind == "module"
    )
    assert entry.state == "skipped"
    assert expected in (entry.reason or "")
    assert entry.member is None


async def test_a_module_that_dies_mid_export_is_skipped_and_the_rest_still_lands(
    tmp_path: Path,
) -> None:
    """Best-effort per module: one failed stream must not cost the operator everything else."""
    engine = await _engine(tmp_path)
    await _seed_core(engine)
    service = _service(
        tmp_path,
        engine,
        snaps=[_snapshot("calendar"), _snapshot("notes")],
        # ``notes`` is healthy in the snapshot but has no base — it went down in between.
        bases={"calendar": "http://calendar:8080"},
        streams={"http://calendar:8080": _module_stream("calendar/1", [])},
    )
    await service._jobs.init()
    await service._files.ensure_tenant_root(tenant=TENANT)

    try:
        started = await service.start_export(tenant=TENANT)
        job = await _settle(service, TENANT, started.id, "running")
    finally:
        await engine.dispose()

    assert job.status == "ready", job.error
    by_name = {c.name: c for c in ArchiveManifest.model_validate(job.manifest).components}
    assert by_name["calendar"].state == "included"
    assert by_name["notes"].state == "skipped"
    assert by_name["notes"].error
    assert by_name["conversations"].state == "included"


async def test_no_secret_material_reaches_the_archive(tmp_path: Path) -> None:
    """The guarantee, checked the blunt way: grep the produced bytes for the fixture's keys."""
    engine = await _engine(tmp_path)
    await _seed_core(engine)
    service = _service(tmp_path, engine, facts=FakeFacts(["a fact"]))
    await service._jobs.init()
    await service._files.ensure_tenant_root(tenant=TENANT)
    # Even a file whose *name* looks like a credential travels as data, never as a key.
    await _write_file(service._files, "notes/keys.md", b"see the vault")

    try:
        started = await service.start_export(tenant=TENANT)
        job = await _settle(service, TENANT, started.id, "running")
        raw = gzip.decompress(Path(job.archive_path or "").read_bytes())
    finally:
        await engine.dispose()

    assert PROVIDER_KEY_VALUE.encode() not in raw
    assert OAUTH_TOKEN_VALUE.encode() not in raw
    # A module's own credential is held to the same rule (#875): the OpenBao *path* travels
    # in the inventory, the bot token behind it never does.
    assert BRIDGE_TOKEN_VALUE.encode() not in raw
    # What *does* travel is the name, so the import can say what to re-enter.
    assert b"openai" in raw
    assert b"messaging/discord" in raw


async def test_a_multi_megabyte_module_stream_is_written_through_to_the_archive(
    tmp_path: Path,
) -> None:
    """Nothing about a large corpus should behave differently — it is streamed, not held."""
    engine = await _engine(tmp_path)
    records = [
        {"kind": "event", "id": f"e-{i}", "data": {"blob": "x" * 1000}} for i in range(3_000)
    ]
    stream = _module_stream("calendar/1", records)
    assert len(stream) > 3 * 1024 * 1024
    service = _service(
        tmp_path,
        engine,
        snaps=[_snapshot("calendar")],
        bases={"calendar": "http://calendar:8080"},
        streams={"http://calendar:8080": stream},
    )
    await service._jobs.init()
    await service._files.ensure_tenant_root(tenant=TENANT)

    try:
        started = await service.start_export(tenant=TENANT)
        job = await _settle(service, TENANT, started.id, "running")
        assert job.status == "ready", job.error
        async with ArchiveReader(Path(job.archive_path or "")) as reader:
            assert await reader.count_records("modules/calendar.ndjson") == 3_000
    finally:
        await engine.dispose()


async def test_a_file_over_the_per_file_limit_is_omitted_and_named(tmp_path: Path) -> None:
    engine = await _engine(tmp_path)
    service = _service(tmp_path, engine, max_file_bytes=16)
    await service._jobs.init()
    await service._files.ensure_tenant_root(tenant=TENANT)
    await _write_file(service._files, "small.txt", b"tiny")
    await _write_file(service._files, "big.bin", b"z" * 64)

    try:
        started = await service.start_export(tenant=TENANT)
        job = await _settle(service, TENANT, started.id, "running")
        assert job.status == "ready", job.error
        async with ArchiveReader(Path(job.archive_path or "")) as reader:
            assert [p for p, _ in reader.file_members()] == ["small.txt"]
    finally:
        await engine.dispose()

    files = next(
        c for c in ArchiveManifest.model_validate(job.manifest).components if c.kind == "files"
    )
    assert files.count == 1
    assert "big.bin" in (files.reason or "")


# ── preview ───────────────────────────────────────────────────────────────────


async def _export_archive(service: TestService) -> Path:
    started = await service.start_export(tenant=TENANT)
    job = await _settle(service, TENANT, started.id, "running")
    assert job.status == "ready", job.error
    return Path(job.archive_path or "")


async def _upload(service: PortabilityService, path: Path) -> Any:
    async def chunks() -> AsyncIterator[bytes]:
        yield path.read_bytes()

    return await service.stage_import(tenant=TENANT, chunks=chunks(), max_bytes=0)


async def test_preview_grades_each_component_and_applies_nothing(tmp_path: Path) -> None:
    engine = await _engine(tmp_path)
    await _seed_core(engine)
    exporter = _service(
        tmp_path / "src",
        engine,
        snaps=[_snapshot("calendar")],
        bases={"calendar": "http://calendar:8080"},
        streams={
            "http://calendar:8080": _module_stream(
                "calendar/2", [{"kind": "event", "id": "e-1", "data": {}}]
            )
        },
    )
    await exporter._jobs.init()
    await exporter._files.ensure_tenant_root(tenant=TENANT)
    try:
        archive = await _export_archive(exporter)

        # The target speaks calendar/3 — a *newer* schema than the archive's calendar/2.
        target = _service(
            tmp_path / "dst",
            engine,
            snaps=[_snapshot("calendar")],
            bases={"calendar": "http://calendar:8080"},
            streams={"http://calendar:8080": _module_stream("calendar/3", [])},
        )
        job = await _upload(target, archive)
        preview = ImportPreview.model_validate(job.preview)
    finally:
        await engine.dispose()

    assert job.status == "staged"
    assert preview.compatible is True
    by_name = {c.name: c for c in preview.components}
    assert by_name["conversations"].verdict == "ok"
    assert by_name["conversations"].records == 1
    # calendar/2 into calendar/3: older, so accepted with a warning.
    assert by_name["calendar"].verdict == "warning"
    assert "older schema" in (by_name["calendar"].detail or "")


async def test_preview_refuses_a_module_that_speaks_an_older_schema_than_the_archive(
    tmp_path: Path,
) -> None:
    engine = await _engine(tmp_path)
    exporter = _service(
        tmp_path / "src",
        engine,
        snaps=[_snapshot("calendar")],
        bases={"calendar": "http://calendar:8080"},
        streams={"http://calendar:8080": _module_stream("calendar/5", [])},
    )
    await exporter._jobs.init()
    await exporter._files.ensure_tenant_root(tenant=TENANT)
    try:
        archive = await _export_archive(exporter)
        target = _service(
            tmp_path / "dst",
            engine,
            snaps=[_snapshot("calendar")],
            bases={"calendar": "http://calendar:8080"},
            streams={"http://calendar:8080": _module_stream("calendar/1", [])},
        )
        job = await _upload(target, archive)
        preview = ImportPreview.model_validate(job.preview)
    finally:
        await engine.dispose()

    calendar = next(c for c in preview.components if c.name == "calendar")
    assert calendar.verdict == "refused"
    # One refused component does not condemn the archive.
    assert preview.compatible is True


async def test_preview_refuses_a_module_this_install_does_not_have(tmp_path: Path) -> None:
    engine = await _engine(tmp_path)
    exporter = _service(
        tmp_path / "src",
        engine,
        snaps=[_snapshot("calendar")],
        bases={"calendar": "http://calendar:8080"},
        streams={"http://calendar:8080": _module_stream("calendar/1", [])},
    )
    await exporter._jobs.init()
    await exporter._files.ensure_tenant_root(tenant=TENANT)
    try:
        archive = await _export_archive(exporter)
        target = _service(tmp_path / "dst", engine)  # no modules at all
        job = await _upload(target, archive)
        preview = ImportPreview.model_validate(job.preview)
    finally:
        await engine.dispose()

    calendar = next(c for c in preview.components if c.name == "calendar")
    assert calendar.verdict == "refused"
    assert "not installed" in (calendar.detail or "")


async def test_a_format_version_mismatch_refuses_the_whole_archive(tmp_path: Path) -> None:
    engine = await _engine(tmp_path)
    service = _service(tmp_path, engine)
    await service._jobs.init()
    await service._files.ensure_tenant_root(tenant=TENANT)

    archive = tmp_path / "future.tar.gz"
    manifest = {
        "format_version": PORTABILITY_FORMAT_VERSION + 1,
        "tenant": TENANT,
        "created_at": WHEN.isoformat(),
        "core_app_version": "9.0.0",
        "epicurus_core_version": "9.0.0",
        "components": [],
        "exclusions": [],
        "secrets": {"provider_keys": [], "connected_accounts": []},
    }
    with tarfile.open(archive, "w:gz") as tar:
        payload = json.dumps(manifest).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))

    try:
        job = await _upload(service, archive)
        preview = ImportPreview.model_validate(job.preview)
        assert preview.compatible is False
        assert "format version" in (preview.refusal or "")
        # The refusal is enforced at the route (see test_portability_routes.py) so the
        # operator is stopped before an apply, not halfway through one.
    finally:
        await engine.dispose()


def test_unsafe_member_names_are_rejected() -> None:
    """Tar-slip, at the door: nothing outside the archive's own four prefixes is readable."""
    assert sanitize_member("files/notes/a.md") == "files/notes/a.md"
    assert sanitize_member("core/prefs.ndjson") == "core/prefs.ndjson"
    assert sanitize_member("manifest.json") == "manifest.json"
    assert sanitize_member("../../etc/passwd") is None
    assert sanitize_member("/etc/passwd") is None
    assert sanitize_member("files/../../etc/passwd") is None
    assert sanitize_member("files\\..\\..\\windows") is None
    assert sanitize_member("secrets/openbao.json") is None
    assert sanitize_member("") is None


# ── apply ─────────────────────────────────────────────────────────────────────


async def test_apply_restores_core_rows_files_and_facts_then_rebuilds(tmp_path: Path) -> None:
    """The move, in one test: export a populated tenant, wipe it, apply, and check it back."""
    engine = await _engine(tmp_path)
    await _seed_core(engine)
    facts = FakeFacts(["the user prefers mornings"])
    rescans: list[tuple[bool, str | None]] = []
    service = _service(
        tmp_path,
        engine,
        snaps=[_snapshot("calendar")],
        bases={"calendar": "http://calendar:8080"},
        streams={
            "http://calendar:8080": _module_stream(
                "calendar/1", [{"kind": "event", "id": "e-1", "data": {"title": "lunch"}}]
            )
        },
        facts=facts,
        rescans=rescans,
    )
    await service._jobs.init()
    await service._files.ensure_tenant_root(tenant=TENANT)
    await _write_file(service._files, "notes/hello.md", b"# hello")

    try:
        archive = await _export_archive(service)

        # Wipe the destination: empty tables, no facts, no files.
        async with engine.begin() as conn:
            for specs in CORE_SETS.values():
                for spec in specs:
                    await conn.execute(spec.table.delete())
        facts.facts.clear()
        await service._files.delete(tenant=TENANT, path="notes/hello.md")

        job = await _upload(service, archive)
        await service.start_apply(tenant=TENANT, job_id=job.id)
        done = await _settle(service, TENANT, job.id, "running")
        assert done.status == "done", done.error
        report = ImportReportView.model_validate(done.report)

        by_name = {c.name: c for c in report.components}
        assert by_name["conversations"].created == 1
        assert by_name["prefs"].created == 1
        assert by_name["memory"].created == 1
        assert by_name["calendar"].created == 1
        assert report.files.written == 1
        assert await service._files.read_bytes(tenant=TENANT, path="notes/hello.md") == b"# hello"
        assert [f.text for f in facts.facts] == ["the user prefers mornings"]
        # The rebuilds the archive deliberately depends on, both reported — and the rescan
        # is forced *for this import's tenant*, never the deployment default (constraint #1).
        assert rescans == [(True, TENANT)]
        assert report.rescan_entries == 7
        assert report.rescan_forced is True
        assert report.reembed == [{"module": "knowledge", "status": "started"}]
        # And the operator is told what to re-enter, at the moment it matters — including
        # the credentials of modules that carried no rows at all (#875).
        assert report.reenter_secrets.provider_keys == ["openai"]
        assert report.reenter_secrets.connected_accounts == ["google"]
        assert report.reenter_secrets.module_secrets == {
            "messaging": ["messaging/discord", "messaging/telegram"]
        }
        # The module received the source's own stream, header line and all.
        sent = service.calls.imports["http://calendar:8080"]
        assert json.loads(sent.splitlines()[0])["schema"] == "calendar/1"
    finally:
        await engine.dispose()


async def test_applying_the_same_archive_twice_changes_nothing(tmp_path: Path) -> None:
    engine = await _engine(tmp_path)
    await _seed_core(engine)
    facts = FakeFacts(["a durable fact"])
    service = _service(tmp_path, engine, facts=facts)
    await service._jobs.init()
    await service._files.ensure_tenant_root(tenant=TENANT)
    await _write_file(service._files, "notes/hello.md", b"# hello")

    try:
        archive = await _export_archive(service)
        reports = []
        for _ in range(2):
            job = await _upload(service, archive)
            await service.start_apply(tenant=TENANT, job_id=job.id)
            done = await _settle(service, TENANT, job.id, "running")
            assert done.status == "done", done.error
            reports.append(ImportReportView.model_validate(done.report))

        for report in reports:
            by_name = {c.name: c for c in report.components}
            # Nothing was created either time — the rows and the file were already there.
            assert by_name["conversations"].created == 0
            assert by_name["conversations"].skipped == 1
            assert by_name["memory"].skipped == 1
            assert report.files.written == 0
            assert report.files.skipped == 1
            assert report.files.conflicts == []
        assert len(facts.facts) == 1
    finally:
        await engine.dispose()


async def test_a_file_that_differs_is_never_overwritten_and_is_named_as_a_conflict(
    tmp_path: Path,
) -> None:
    engine = await _engine(tmp_path)
    service = _service(tmp_path, engine)
    await service._jobs.init()
    await service._files.ensure_tenant_root(tenant=TENANT)
    await _write_file(service._files, "notes/hello.md", b"original")

    try:
        archive = await _export_archive(service)
        await _write_file(service._files, "notes/hello.md", b"edited since the export")

        job = await _upload(service, archive)
        await service.start_apply(tenant=TENANT, job_id=job.id)
        done = await _settle(service, TENANT, job.id, "running")
        report = ImportReportView.model_validate(done.report)

        assert report.files.conflicts == ["notes/hello.md"]
        assert report.files.written == 0
        assert (
            await service._files.read_bytes(tenant=TENANT, path="notes/hello.md")
            == b"edited since the export"
        )
    finally:
        await engine.dispose()


async def test_a_refused_module_is_skipped_while_the_rest_of_the_archive_applies(
    tmp_path: Path,
) -> None:
    engine = await _engine(tmp_path)
    await _seed_core(engine)
    exporter = _service(
        tmp_path / "src",
        engine,
        snaps=[_snapshot("calendar")],
        bases={"calendar": "http://calendar:8080"},
        streams={
            "http://calendar:8080": _module_stream(
                "calendar/1", [{"kind": "event", "id": "e-1", "data": {}}]
            )
        },
    )
    await exporter._jobs.init()
    await exporter._files.ensure_tenant_root(tenant=TENANT)
    try:
        archive = await _export_archive(exporter)
        async with engine.begin() as conn:
            for specs in CORE_SETS.values():
                for spec in specs:
                    await conn.execute(spec.table.delete())

        target = _service(tmp_path / "dst", engine)  # calendar is not installed here
        await target._files.ensure_tenant_root(tenant=TENANT)
        job = await _upload(target, archive)
        await target.start_apply(tenant=TENANT, job_id=job.id)
        done = await _settle(target, TENANT, job.id, "running")
        report = ImportReportView.model_validate(done.report)
    finally:
        await engine.dispose()

    assert done.status == "done", done.error
    by_name = {c.name: c for c in report.components}
    assert by_name["calendar"].state == "skipped"
    assert by_name["conversations"].created == 1  # the rest landed


# ── jobs & staging ────────────────────────────────────────────────────────────


async def test_a_job_is_tenant_scoped(tmp_path: Path) -> None:
    engine = await _engine(tmp_path)
    service = _service(tmp_path, engine)
    await service._jobs.init()
    await service._files.ensure_tenant_root(tenant=TENANT)
    try:
        started = await service.start_export(tenant=TENANT)
        await _settle(service, TENANT, started.id, "running")
        assert await service.job(tenant="other", job_id=started.id) is None
    finally:
        await engine.dispose()


async def test_the_sweep_drops_a_staged_archive_past_its_retention(tmp_path: Path) -> None:
    engine = await _engine(tmp_path)
    service = _service(tmp_path, engine)
    service._retention = service._retention * 0  # everything is instantly expired
    await service._jobs.init()
    await service._files.ensure_tenant_root(tenant=TENANT)
    try:
        started = await service.start_export(tenant=TENANT)
        job = await _settle(service, TENANT, started.id, "running")
        archive = Path(job.archive_path or "")
        assert archive.exists()

        await service.sweep(TENANT)

        assert not archive.exists()
        assert await service.job(tenant=TENANT, job_id=job.id) is None
    finally:
        await engine.dispose()


# ── the modules' own credentials (#875) ───────────────────────────────────────


class FakeVault:
    """A secret store with only what the inventory needs: is there something at this path?

    Records every probe so a test can assert the *tenant* was threaded (constraint #1) and
    that nothing more than presence was ever asked for.
    """

    def __init__(self, present: dict[str, set[str]] | None = None, *, broken: bool = False) -> None:
        self._present = present or {}
        self._broken = broken
        self.probes: list[tuple[str, str | None]] = []

    async def get(self, path: str, tenant_id: str | None = None) -> dict[str, Any]:
        self.probes.append((path, tenant_id))
        if self._broken:
            raise RuntimeError("openbao is unreachable")
        if path not in self._present.get(tenant_id or "", set()):
            raise KeyError(f"secret not found: {path}")
        return {"token": BRIDGE_TOKEN_VALUE}


class BrokenRegistry:
    async def snapshot(self, *, force: bool = False) -> list[ModuleSnapshot]:
        raise RuntimeError("the module registry is not up")


async def test_module_secrets_name_only_the_paths_this_tenant_actually_holds() -> None:
    """The point of #875: `messaging` carries no rows and still has to be reconnected."""
    registry = FakeRegistry(
        [
            _snapshot(
                "messaging",
                portable=False,
                secrets=["messaging/discord", "messaging/telegram"],
            ),
            _snapshot("calendar", secrets=[]),
        ],
        {},
    )
    vault = FakeVault({TENANT: {"messaging/discord"}})

    found = await collect_module_secrets(registry, vault, tenant=TENANT)

    # Only what is there — an unconnected bridge is not something to reconnect.
    assert found == {"messaging": ["messaging/discord"]}
    # Every probe was tenant-scoped, and the module that declares nothing was not probed.
    assert vault.probes == [
        ("messaging/discord", TENANT),
        ("messaging/telegram", TENANT),
    ]


async def test_module_secrets_ignore_disabled_and_removed_modules() -> None:
    """A module the operator turned off (or removed) is not a reconnect the move needs."""
    registry = FakeRegistry(
        [
            _snapshot("messaging", enabled=False, secrets=["messaging/discord"]),
            _snapshot("gone", removed=True, secrets=["gone/token"]),
        ],
        {},
    )
    vault = FakeVault({TENANT: {"messaging/discord", "gone/token"}})

    assert await collect_module_secrets(registry, vault, tenant=TENANT) == {}
    assert vault.probes == []


async def test_module_secrets_are_scoped_to_the_tenant_being_exported() -> None:
    """Another tenant's connected bridge is not this tenant's inventory."""
    registry = FakeRegistry([_snapshot("messaging", secrets=["messaging/discord"])], {})
    vault = FakeVault({"other": {"messaging/discord"}})

    assert await collect_module_secrets(registry, vault, tenant=TENANT) == {}
    assert await collect_module_secrets(registry, vault, tenant="other") == {
        "messaging": ["messaging/discord"]
    }


async def test_an_inventory_that_cannot_be_taken_is_empty_not_fatal() -> None:
    """Best-effort, like the rest of the inventory: an archive is worth having without it."""
    registry = FakeRegistry([_snapshot("messaging", secrets=["messaging/discord"])], {})

    assert await collect_module_secrets(registry, FakeVault(broken=True), tenant=TENANT) == {}
    assert await collect_module_secrets(BrokenRegistry(), FakeVault(), tenant=TENANT) == {}
