"""The tenant export/import orchestrator (#867).

The core is the only thing that can see a whole tenant: its own tables, the file space it
owns, and every module's answer to the portability contract. This assembles those three
into one archive, and applies one back.

Three properties the implementation is built around, in order of importance:

* **An import never destroys.** Every write is an upsert by stable id; nothing here deletes
  a row or overwrites a file whose bytes differ. Applying the same archive twice is a no-op,
  and applying an archive into a *populated* install merges rather than replaces. That is
  what makes "try it and see" a safe instruction.
* **One component's failure is one component's failure.** A module that is down, disabled,
  or speaking a schema this installation cannot read is recorded and stepped over — an
  operator moving house does not lose their conversations because the mail container is
  restarting.
* **No component is ever wholly in memory.** Each one is streamed to its own staged part file
  and handed to tar; an NDJSON member is read back line by line and a module's member is
  proxied to it chunk by chunk. The one exception is a *file*, which both halves read whole
  because :class:`~epicurus_core.files.FileStore` has no chunked read — which is exactly why
  the per-file ceiling (``PORTABILITY_MAX_FILE_MB``) exists rather than being optional polish,
  and why it can go away the day the store grows a ``read_stream``.

Staging is a **disposable cache directory** (constraint #2): the archive is a build artefact,
reproducible by pressing Export again, and the durable state is the job row that points at
it. For a multi-replica SaaS that directory becomes object storage — the seam is
``staging_dir`` and nothing else assumes local disk (see the ADR).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import httpx

from epicurus_core import (
    FileStore,
    ImportReport,
    ModuleManifest,
    get_logger,
    schema_verdict,
)
from epicurus_core import (
    __version__ as epicurus_core_version,
)
from epicurus_core_app.portability import core_data
from epicurus_core_app.portability.archive import (
    ArchiveReader,
    ArchiveWriter,
    core_member,
    files_member,
    module_member,
)
from epicurus_core_app.portability.core_data import CORE_SCHEMA, CORE_SETS, EXCLUSIONS, MEMORY_SET
from epicurus_core_app.portability.jobs import PortabilityJob, PortabilityJobStore
from epicurus_core_app.portability.models import (
    ARCHIVE_MANIFEST_MEMBER,
    CORE_MEMBER_PREFIX,
    MODULE_MEMBER_PREFIX,
    PORTABILITY_FORMAT_VERSION,
    ArchiveManifest,
    ComponentEntry,
    FileTransfer,
    ImportComponentPreview,
    ImportComponentResult,
    ImportPreview,
    ImportReportView,
    SecretsInventory,
    as_json,
)

__all__ = ["ArchiveTooLarge", "FactSource", "ModuleTargets", "PortabilityService"]


class ArchiveTooLarge(ValueError):
    """An upload exceeded the configured archive ceiling.

    Its own type, not a bare ``ValueError``: the archive reader raises ``ValueError`` too
    (``MemberError`` subclasses it), and "too big" is a 413 while "not an archive" is a 400.
    Collapsing them once already turned a corrupt upload into a size complaint.
    """


log = get_logger("core.portability")

FILES_COMPONENT = "files"
"""The name the file space travels under in progress, the manifest, and the preview."""

# The memory fact corpus is scrolled, not paged, so the export takes it in one bounded read.
# A personal assistant's fact store is small by construction (ADR-0045); above this the
# export says so in a warning rather than silently truncating.
_FACT_CAP = 10_000


class ModuleTargets(Protocol):
    """The slice of :class:`~epicurus_core_app.modules.ModuleRegistry` this needs.

    Structural, so the registry satisfies it without knowing about portability, and a test
    can stand in two methods instead of a fleet.
    """

    async def snapshot(self, *, force: bool = False) -> Sequence[Any]: ...

    async def base_url(self, name: str) -> str: ...


class FactSource(Protocol):
    """The memory store's own read/write surface for durable facts (never Qdrant points)."""

    async def list_facts(
        self, *, tenant: str, limit: int = 200, cap: int = 2000
    ) -> Sequence[Any]: ...

    async def save(self, *, tenant: str, text: str, source: str = "auto") -> Any: ...


class PortabilityService:
    """Runs tenant exports and imports as durable, tenant-scoped background jobs."""

    def __init__(
        self,
        *,
        jobs: PortabilityJobStore,
        engine: Any,
        file_store: FileStore,
        registry: ModuleTargets,
        staging_dir: Path,
        core_app_version: str,
        facts: FactSource | None = None,
        secrets_inventory: Callable[[str], Awaitable[SecretsInventory]] | None = None,
        rescan: Callable[..., Awaitable[int]] | None = None,
        reembed: Callable[[], Awaitable[list[dict[str, str]]]] | None = None,
        max_file_bytes: int = 512 * 1024 * 1024,
        retention_hours: int = 24,
        request_timeout: float = 600.0,
    ) -> None:
        self._jobs = jobs
        self._engine = engine
        self._files = file_store
        self._registry = registry
        self._staging = staging_dir
        self._core_app_version = core_app_version
        self._facts = facts
        self._secrets_inventory = secrets_inventory
        self._rescan = rescan
        self._reembed = reembed
        self._max_file_bytes = max_file_bytes
        self._retention = timedelta(hours=retention_hours)
        self._timeout = request_timeout
        # Background jobs are detached from the request that started them (a browser tab may
        # be closed the moment after). Hold a reference so the loop cannot garbage-collect a
        # running task out from under the operator — the #376 lesson, in miniature.
        self._tasks: set[asyncio.Task[None]] = set()

    # ── staging ──────────────────────────────────────────────────────────────

    async def job(self, *, tenant: str, job_id: str) -> PortabilityJob | None:
        """One job of this tenant's, or ``None`` — the routes' single read path."""
        return await self._jobs.get(tenant=tenant, job_id=job_id)

    def job_dir(self, tenant: str, job_id: str) -> Path:
        """This job's own directory in the disposable staging area (tenant-scoped)."""
        return self._staging / tenant / job_id

    async def sweep(self, tenant: str) -> int:
        """Drop staged archives (and their rows) older than the retention window.

        Runs when a job starts rather than on a timer: the only thing that grows the staging
        directory is starting jobs, so that is exactly when it is worth tidying, and it keeps
        the service free of a background loop to own.
        """
        cutoff = datetime.now(UTC) - self._retention
        removed = 0
        for job in await self._jobs.expired(tenant=tenant, before=cutoff):
            await asyncio.to_thread(
                shutil.rmtree, str(self.job_dir(tenant, job.id)), ignore_errors=True
            )
            await self._jobs.delete(tenant=tenant, job_id=job.id)
            removed += 1
        if removed:
            log.info("swept expired portability jobs", tenant=tenant, jobs=removed)
        return removed

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ── export ───────────────────────────────────────────────────────────────

    async def start_export(self, *, tenant: str) -> PortabilityJob:
        """Open an export job and run it in the background; returns the job immediately."""
        await self.sweep(tenant)
        plan = await self._export_plan(tenant)
        job = await self._jobs.create(tenant=tenant, kind="export", status="running")
        await self._jobs.update(tenant=tenant, job_id=job.id, progress=[as_json(e) for e in plan])
        self._spawn(self._run_export(tenant, job.id, plan))
        return job

    async def _export_plan(self, tenant: str) -> list[ComponentEntry]:
        """Every component the export will attempt, in archive order, all ``pending``.

        Built up front so the UI has the full list from the first poll — a progress display
        that grows a row at a time cannot show how far along it is.
        """
        plan = [
            ComponentEntry(
                name=name,
                kind="core",
                schema_name=CORE_SCHEMA,
                version=self._core_app_version,
                member=core_member(name),
            )
            for name in (*CORE_SETS, MEMORY_SET)
        ]
        for snap in await self._registry.snapshot():
            manifest: ModuleManifest = snap.manifest
            if getattr(snap, "removed", False):
                continue
            entry = ComponentEntry(
                name=manifest.name,
                kind="module",
                version=manifest.version,
                member=module_member(manifest.name),
            )
            if not manifest.portable:
                entry.state = "skipped"
                entry.reason = "module does not declare portable data"
                entry.member = None
            elif not getattr(snap, "enabled", True):
                entry.state = "skipped"
                entry.reason = "module is disabled"
                entry.member = None
            elif not snap.status.healthy:
                entry.state = "skipped"
                entry.reason = "module is unreachable"
                entry.member = None
            plan.append(entry)
        plan.append(ComponentEntry(name=FILES_COMPONENT, kind="files"))
        return plan

    async def _run_export(self, tenant: str, job_id: str, plan: list[ComponentEntry]) -> None:
        """The export job itself: stage every component, then assemble the archive."""
        directory = self.job_dir(tenant, job_id)
        parts = directory / "parts"
        archive_path = directory / "archive.tar.gz"
        try:
            await asyncio.to_thread(parts.mkdir, parents=True, exist_ok=True)
            staged: dict[str, Path] = {}
            for entry in plan:
                if entry.state == "skipped":
                    continue
                entry.state = "running"
                await self._save_progress(tenant, job_id, plan)
                try:
                    if entry.kind == "core":
                        part = parts / f"core-{entry.name}.ndjson"
                        entry.count = await self._stage_core_set(tenant, entry.name, part, entry)
                        staged[entry.member or core_member(entry.name)] = part
                        entry.state = "included"
                    elif entry.kind == "module":
                        part = parts / f"module-{entry.name}.ndjson"
                        base = await self._registry.base_url(entry.name)
                        entry.count = await self._stream_module_export(base, tenant, part)
                        staged[entry.member or module_member(entry.name)] = part
                        entry.state = "included"
                    else:
                        entry.state = "included"  # the file space is copied during assembly
                except Exception as exc:
                    # A module that went down between the plan and the call, a set whose table
                    # is missing — recorded, never fatal. The archive is still worth having.
                    entry.state = "skipped" if entry.kind == "module" else "failed"
                    entry.error = f"{type(exc).__name__}: {exc}"
                    entry.reason = entry.reason or "component could not be exported"
                    log.warning(
                        "portability export component failed",
                        component=entry.name,
                        kind=entry.kind,
                        error=str(exc),
                    )
                await self._save_progress(tenant, job_id, plan)

            # The file half is planned before the manifest is written, not during assembly:
            # ``manifest.json`` is the archive's first member, so everything it reports has to
            # be known before a single file is copied. The walk gives sizes, which is all the
            # per-file limit needs to decide.
            file_entry = next(e for e in plan if e.kind == "files")
            file_entry.state = "included"
            eligible: list[tuple[str, int]] = []
            oversized: list[str] = []
            for path, size_bytes in await self._walk_files(tenant):
                if self._max_file_bytes and size_bytes > self._max_file_bytes:
                    oversized.append(path)
                else:
                    eligible.append((path, size_bytes))
            file_entry.count = len(eligible)
            if oversized:
                file_entry.reason = (
                    f"{len(oversized)} file(s) above the per-file export limit were omitted: "
                    + ", ".join(oversized[:5])
                )
            manifest = ArchiveManifest(
                format_version=PORTABILITY_FORMAT_VERSION,
                tenant=tenant,
                created_at=datetime.now(UTC).isoformat(),
                core_app_version=self._core_app_version,
                epicurus_core_version=epicurus_core_version,
                components=plan,
                exclusions=list(EXCLUSIONS),
                secrets=await self._collect_secrets(tenant),
            )
            copied = await self._assemble(archive_path, manifest, staged, tenant, eligible)
            if copied != len(eligible):
                # A file deleted between the walk and the read. The members are the truth —
                # a reader counts them rather than trusting the manifest — but say so.
                file_entry.reason = (
                    f"{len(eligible) - copied} file(s) disappeared during the export"
                )
                file_entry.count = copied
            size = await asyncio.to_thread(lambda: archive_path.stat().st_size)
            await self._jobs.update(
                tenant=tenant,
                job_id=job_id,
                status="ready",
                progress=[as_json(e) for e in plan],
                manifest=as_json(manifest),
                archive_path=str(archive_path),
                size_bytes=size,
            )
            log.info(
                "portability export ready",
                tenant=tenant,
                job=job_id,
                bytes=size,
                components=len(plan),
            )
        except Exception as exc:
            log.error("portability export failed", tenant=tenant, job=job_id, error=str(exc))
            await self._jobs.update(
                tenant=tenant,
                job_id=job_id,
                status="failed",
                progress=[as_json(e) for e in plan],
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _save_progress(self, tenant: str, job_id: str, plan: list[ComponentEntry]) -> None:
        await self._jobs.update(tenant=tenant, job_id=job_id, progress=[as_json(e) for e in plan])

    async def _stage_core_set(
        self, tenant: str, set_name: str, target: Path, entry: ComponentEntry
    ) -> int:
        """Write one core set to *target* as NDJSON (header line first); returns its count."""
        count = 0

        def _open() -> Any:
            return target.open("wb")

        handle = await asyncio.to_thread(_open)
        try:
            await asyncio.to_thread(
                handle.write,
                _line({"schema": CORE_SCHEMA, "component_version": self._core_app_version}),
            )
            if set_name == MEMORY_SET:
                async for payload in self._memory_records(tenant, entry):
                    await asyncio.to_thread(handle.write, _line(payload))
                    count += 1
            else:
                async for record in core_data.export_set(self._engine, set_name, tenant=tenant):
                    await asyncio.to_thread(handle.write, _line(record.model_dump()))
                    count += 1
        finally:
            await asyncio.to_thread(handle.close)
        return count

    async def _memory_records(
        self, tenant: str, entry: ComponentEntry
    ) -> AsyncIterator[dict[str, Any]]:
        """Durable facts as records — the fact's own text and metadata, never its vector."""
        if self._facts is None:
            return
        facts = await self._facts.list_facts(tenant=tenant, limit=_FACT_CAP, cap=_FACT_CAP)
        if len(facts) >= _FACT_CAP:
            entry.reason = f"only the first {_FACT_CAP} facts were exported (the corpus is larger)"

        for fact in facts:
            yield {
                "kind": "memory_fact",
                "id": str(fact.id),
                "data": {
                    "text": fact.text,
                    "source": fact.source,
                    "created_at": fact.created_at.isoformat() if fact.created_at else None,
                },
            }

    async def _walk_files(self, tenant: str) -> list[tuple[str, int]]:
        """Every file in the tenant's own file space, walked through the store API.

        Through :class:`~epicurus_core.files.FileStore`, not the filesystem and not the index:
        the store is the backend-agnostic seam (local-FS ↔ S3, constraint #3), and unlike the
        index it cannot be stale or have its purge fused off (#848). External mounts are
        **not** walked — they are the operator's own directories, mounted by this deployment's
        compose file, not the tenant's data.
        """
        found: list[tuple[str, int]] = []
        stack = [""]
        while stack:
            directory = stack.pop()
            for entry in await self._files.list_dir(tenant=tenant, path=directory):
                if entry.kind == "dir":
                    stack.append(entry.path)
                else:
                    found.append((entry.path, entry.size))
        found.sort()
        return found

    async def _collect_secrets(self, tenant: str) -> SecretsInventory:
        """What the source holds in OpenBao — names only, never material."""
        if self._secrets_inventory is None:
            return SecretsInventory()
        try:
            return await self._secrets_inventory(tenant)
        except Exception as exc:  # an inventory we could not take is not a failed export
            log.warning("portability secret inventory failed", tenant=tenant, error=str(exc))
            return SecretsInventory()

    async def _assemble(
        self,
        archive_path: Path,
        manifest: ArchiveManifest,
        staged: dict[str, Path],
        tenant: str,
        files: list[tuple[str, int]],
    ) -> int:
        """Write the archive — manifest, staged parts, then the file space — and count files."""
        copied = 0
        async with ArchiveWriter(archive_path) as writer:
            await writer.add_bytes(
                ARCHIVE_MANIFEST_MEMBER,
                json.dumps(manifest.model_dump(by_alias=True, mode="json"), indent=2).encode(),
            )
            for member, part in staged.items():
                await writer.add_path(member, part)
            for path, _size in files:
                try:
                    data = await self._files.read_bytes(tenant=tenant, path=path)
                except FileNotFoundError:
                    continue  # deleted between the walk and the read — not an error
                await writer.add_bytes(files_member(path), data)
                copied += 1
        return copied

    # ── import ───────────────────────────────────────────────────────────────

    async def stage_import(
        self, *, tenant: str, chunks: AsyncIterator[bytes], max_bytes: int
    ) -> PortabilityJob:
        """Save an uploaded archive to staging and build its preview. Nothing is applied."""
        await self.sweep(tenant)
        job = await self._jobs.create(tenant=tenant, kind="import", status="staged")
        directory = self.job_dir(tenant, job.id)
        archive_path = directory / "archive.tar.gz"
        await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
        written = 0
        handle = await asyncio.to_thread(archive_path.open, "wb")
        try:
            async for chunk in chunks:
                written += len(chunk)
                if max_bytes and written > max_bytes:
                    raise ArchiveTooLarge(f"archive exceeds the {max_bytes}-byte upload limit")
                await asyncio.to_thread(handle.write, chunk)
        except Exception:
            await asyncio.to_thread(handle.close)
            await asyncio.to_thread(shutil.rmtree, str(directory), ignore_errors=True)
            await self._jobs.delete(tenant=tenant, job_id=job.id)
            raise
        await asyncio.to_thread(handle.close)
        preview = await self._preview(tenant, archive_path)
        await self._jobs.update(
            tenant=tenant,
            job_id=job.id,
            preview=as_json(preview),
            archive_path=str(archive_path),
            size_bytes=written,
        )
        updated = await self._jobs.get(tenant=tenant, job_id=job.id)
        assert updated is not None
        return updated

    async def _preview(self, tenant: str, archive_path: Path) -> ImportPreview:
        """Read the archive and grade every component against this installation."""
        async with ArchiveReader(archive_path) as reader:
            manifest = await reader.manifest()
            if manifest.format_version != PORTABILITY_FORMAT_VERSION:
                # The container's own shape, not a component's: nothing inside can be trusted
                # to mean what this reader thinks it means, so refuse the whole archive.
                return ImportPreview(
                    manifest=manifest,
                    compatible=False,
                    refusal=(
                        f"archive format version {manifest.format_version} cannot be read by "
                        f"this core (format {PORTABILITY_FORMAT_VERSION})"
                    ),
                )
            components: list[ImportComponentPreview] = []
            for member in sorted(reader.ndjson_members(CORE_MEMBER_PREFIX)):
                components.append(await self._preview_core(reader, member))
            for member in sorted(reader.ndjson_members(MODULE_MEMBER_PREFIX)):
                components.append(await self._preview_module(reader, member, tenant))
            file_members = reader.file_members()
            if file_members:
                components.append(
                    ImportComponentPreview(
                        name=FILES_COMPONENT, kind="files", records=len(file_members)
                    )
                )
            preview = ImportPreview(manifest=manifest, components=components)
            if reader.rejected_members:
                preview.refusal = (
                    f"{len(reader.rejected_members)} archive member(s) had unsafe names "
                    "and will be ignored"
                )
            return preview

    async def _preview_core(self, reader: ArchiveReader, member: str) -> ImportComponentPreview:
        name = member[len(CORE_MEMBER_PREFIX) : -len(".ndjson")]
        header = await reader.header(member)
        incoming = header.schema_name if header else CORE_SCHEMA
        verdict = schema_verdict(incoming, CORE_SCHEMA)
        known = name in CORE_SETS or name == MEMORY_SET
        entry = ImportComponentPreview(
            name=name,
            kind="core",
            records=await reader.count_records(member),
            schema_name=incoming,
        )
        if not known:
            entry.verdict = "refused"
            entry.detail = "this core has no such data set"
        elif verdict in ("newer", "foreign"):
            entry.verdict = "refused"
            entry.detail = f"archive schema {incoming} cannot be read by {CORE_SCHEMA}"
        elif verdict == "older":
            entry.verdict = "warning"
            entry.detail = f"written by an older schema ({incoming}); will be upgraded on import"
        return entry

    async def _preview_module(
        self, reader: ArchiveReader, member: str, tenant: str
    ) -> ImportComponentPreview:
        name = member[len(MODULE_MEMBER_PREFIX) : -len(".ndjson")]
        header = await reader.header(member)
        incoming = header.schema_name if header else f"{name}/1"
        entry = ImportComponentPreview(
            name=name,
            kind="module",
            records=await reader.count_records(member),
            schema_name=incoming,
        )
        target = await self._module_target(name)
        if target is None:
            entry.verdict = "refused"
            entry.detail = "module is not installed, not enabled, or not reachable"
            return entry
        base, _ = target
        local = await self._module_schema(base, tenant)
        if local is None:
            entry.verdict = "refused"
            entry.detail = "module did not answer with a portability schema"
            return entry
        verdict = schema_verdict(incoming, local)
        if verdict in ("newer", "foreign"):
            entry.verdict = "refused"
            entry.detail = f"archive schema {incoming} cannot be read by {local}"
        elif verdict == "older":
            entry.verdict = "warning"
            entry.detail = f"written by an older schema ({incoming}); the module will upgrade it"
        return entry

    async def _module_target(self, name: str) -> tuple[str, ModuleManifest] | None:
        """The base URL + manifest of a healthy, enabled, portable module — or ``None``."""
        for snap in await self._registry.snapshot():
            manifest: ModuleManifest = snap.manifest
            if manifest.name != name:
                continue
            if not manifest.portable:
                return None
            if getattr(snap, "removed", False) or not getattr(snap, "enabled", True):
                return None
            if not snap.status.healthy:
                return None
            try:
                return await self._registry.base_url(name), manifest
            except Exception:
                return None
        return None

    async def start_apply(self, *, tenant: str, job_id: str) -> PortabilityJob | None:
        """Begin applying a staged import in the background (``None`` if the job is gone)."""
        job = await self._jobs.get(tenant=tenant, job_id=job_id)
        if job is None:
            return None
        await self._jobs.update(tenant=tenant, job_id=job_id, status="running")
        self._spawn(self._run_apply(tenant, job_id, Path(job.archive_path or "")))
        updated = await self._jobs.get(tenant=tenant, job_id=job_id)
        return updated

    async def _run_apply(self, tenant: str, job_id: str, archive_path: Path) -> None:
        """Apply every accepted component, then rebuild what the archive deliberately omits."""
        report = ImportReportView()
        try:
            async with ArchiveReader(archive_path) as reader:
                manifest = await reader.manifest()
                report.reenter_secrets = manifest.secrets
                for member in sorted(reader.ndjson_members(CORE_MEMBER_PREFIX)):
                    name = member[len(CORE_MEMBER_PREFIX) : -len(".ndjson")]
                    report.components.append(await self._apply_core(reader, member, name, tenant))
                for member in sorted(reader.ndjson_members(MODULE_MEMBER_PREFIX)):
                    name = member[len(MODULE_MEMBER_PREFIX) : -len(".ndjson")]
                    report.components.append(await self._apply_module(reader, member, name, tenant))
                report.files = await self._apply_files(reader, tenant)
            await self._rebuild(report, tenant)
            await self._jobs.update(
                tenant=tenant, job_id=job_id, status="done", report=as_json(report)
            )
            log.info("portability import applied", tenant=tenant, job=job_id)
        except Exception as exc:
            log.error("portability import failed", tenant=tenant, job=job_id, error=str(exc))
            await self._jobs.update(
                tenant=tenant,
                job_id=job_id,
                status="failed",
                report=as_json(report),
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _apply_core(
        self, reader: ArchiveReader, member: str, name: str, tenant: str
    ) -> ImportComponentResult:
        result = ImportComponentResult(name=name, kind="core", state="included")
        header = await reader.header(member)
        incoming = header.schema_name if header else CORE_SCHEMA
        verdict = schema_verdict(incoming, CORE_SCHEMA)
        if name not in CORE_SETS and name != MEMORY_SET:
            return _skipped(result, "this core has no such data set")
        if verdict in ("newer", "foreign"):
            return _skipped(result, f"archive schema {incoming} cannot be read by {CORE_SCHEMA}")
        try:
            if name == MEMORY_SET:
                report = await self._apply_memory(reader, member, tenant)
            else:
                report = await core_data.import_set(
                    self._engine,
                    name,
                    reader.records(member),
                    tenant=tenant,
                    dry_run=False,
                )
        except Exception as exc:
            result.state = "failed"
            result.error = f"{type(exc).__name__}: {exc}"
            log.warning("portability core set failed", component=name, error=str(exc))
            return result
        _fold(result, report)
        if verdict == "older":
            result.warnings.append(f"written by an older schema ({incoming})")
        return result

    async def _apply_memory(self, reader: ArchiveReader, member: str, tenant: str) -> ImportReport:
        """Re-save each exported fact through the memory store, which re-embeds and dedups.

        Idempotency comes free and correct here: ``save`` refuses a fact that is
        near-identical to one already stored (it compares the fresh embedding, not the
        string), so a second apply — or an import into an install that already knows the
        same things — adds nothing.
        """
        report = ImportReport(schema_name=CORE_SCHEMA)
        if self._facts is None:
            report.warn("memory: no fact store wired; facts were not imported")
            return report
        async for record in reader.records(member):
            text = str(record.data.get("text", "")).strip()
            if not text:
                report.record(record.kind, "skipped")
                continue
            saved = await self._facts.save(
                tenant=tenant, text=text, source=str(record.data.get("source", "auto"))
            )
            report.record(record.kind, "skipped" if saved is None else "created")
        return report

    async def _apply_module(
        self, reader: ArchiveReader, member: str, name: str, tenant: str
    ) -> ImportComponentResult:
        result = ImportComponentResult(name=name, kind="module", state="included")
        target = await self._module_target(name)
        if target is None:
            return _skipped(result, "module is not installed, not enabled, or not reachable")
        base, _ = target
        try:
            report = await self._post_module_import(base, tenant, reader.raw_lines(member))
        except httpx.HTTPStatusError as exc:
            # 409 is the module refusing an incompatible stream — its job, its wording.
            detail = _detail(exc.response)
            if exc.response.status_code == 409:
                return _skipped(result, detail or "module refused the stream as incompatible")
            result.state = "failed"
            result.error = detail or str(exc)
            return result
        except Exception as exc:
            result.state = "failed"
            result.error = f"{type(exc).__name__}: {exc}"
            return result
        _fold(result, report)
        return result

    async def _apply_files(self, reader: ArchiveReader, tenant: str) -> FileTransfer:
        """Write the archive's files that are absent here; never overwrite differing bytes.

        A file that already exists with *identical* bytes is a no-op (so a re-apply is
        silent); one that exists with *different* bytes is named as a conflict and left
        exactly as it is. Import is additive, and a file the operator has since edited is
        the clearest case there is of something an import has no business replacing.
        """
        transfer = FileTransfer()
        for path, _size in reader.file_members():
            data = await reader.read_bytes(files_member(path))
            existing = await self._files.stat(tenant=tenant, path=path)
            if existing is None:
                await self._files.write_bytes(tenant=tenant, path=path, data=data)
                transfer.written += 1
                continue
            current = await self._files.read_bytes(tenant=tenant, path=path)
            if current == data:
                transfer.skipped += 1
            else:
                transfer.conflicts.append(path)
        return transfer

    async def _rebuild(self, report: ImportReportView, tenant: str) -> None:
        """Re-derive what the archive deliberately omitted: the file index, then the vectors.

        The rescan is **forced** (#848): a fresh install's index is empty and the imported
        tree is not, which is exactly the wholesale flip the mass de-index fuse exists to
        refuse. Forcing it is the operator's "yes, this really is the new tree" — and
        ``rescan_forced`` on the report says so, so nothing about that is implicit.

        It is also **tenant-scoped** (constraint #1). The core's rescan helper falls back to
        the deployment's default tenant when handed none, so an apply that dropped the tenant
        here would rebuild the wrong tree's index and report its count as this import's. The
        apply knows the tenant all the way down; the last step must not be where it forgets.
        The re-embed fan-out carries no tenant of its own — it is the existing #332 call, and
        each module re-embeds its own tenant's corpus (single-tenant in v1).
        """
        if self._rescan is not None:
            try:
                report.rescan_entries = await self._rescan(force=True, tenant=tenant)
                report.rescan_forced = True
            except Exception as exc:
                report.rescan_error = f"{type(exc).__name__}: {exc}"
                log.warning("portability post-import rescan failed", error=str(exc))
        if self._reembed is not None:
            try:
                report.reembed = await self._reembed()
            except Exception as exc:
                report.reembed_error = f"{type(exc).__name__}: {exc}"
                log.warning("portability post-import re-embed failed", error=str(exc))

    # ── module HTTP (overridable in tests, like ModuleRegistry._post_reindex) ──

    async def _module_schema(self, base: str, tenant: str) -> str | None:
        """The module's current record schema, read from its export stream's header line.

        A peek, not an export: the helper yields the header before touching the store, so the
        stream is closed again before a single record is produced. That is what lets the
        import *preview* grade a module the same way it grades a core set, instead of
        discovering the mismatch halfway through an apply.
        """
        try:
            async with (
                httpx.AsyncClient(timeout=30) as client,
                client.stream("GET", f"{base}/export", params={"tenant_id": tenant}) as response,
            ):
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    header = json.loads(line)
                    value = header.get("schema")
                    return str(value) if value else None
        except Exception as exc:
            log.warning("portability module schema peek failed", base=base, error=str(exc))
        return None

    async def _stream_module_export(self, base: str, tenant: str, target: Path) -> int:
        """Stream ``GET {base}/export`` straight to *target*; returns the record count."""
        handle = await asyncio.to_thread(target.open, "wb")
        newlines = 0
        try:
            async with (
                httpx.AsyncClient(timeout=self._timeout) as client,
                client.stream("GET", f"{base}/export", params={"tenant_id": tenant}) as response,
            ):
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    newlines += chunk.count(b"\n")
                    await asyncio.to_thread(handle.write, chunk)
        finally:
            await asyncio.to_thread(handle.close)
        # Every line is terminated by the helper, and the first is the header.
        return max(newlines - 1, 0)

    async def _post_module_import(
        self, base: str, tenant: str, body: AsyncIterator[bytes]
    ) -> ImportReport:
        """POST the member's own bytes back to ``{base}/import`` — header line included.

        The archive's header travels verbatim rather than being rewritten, so the *module*
        applies the compatibility rule to the stream it is actually being given. The core's
        preview and the module's door then agree by construction instead of by convention.
        """
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{base}/import", params={"tenant_id": tenant}, content=body
            )
            response.raise_for_status()
            return ImportReport.model_validate(response.json())


def _line(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, separators=(",", ":"), default=str) + "\n").encode("utf-8")


def _skipped(result: ImportComponentResult, reason: str) -> ImportComponentResult:
    result.state = "skipped"
    result.reason = reason
    return result


def _fold(result: ImportComponentResult, report: ImportReport) -> None:
    """Collapse a per-kind :class:`ImportReport` into one component row of the final report."""
    for counts in report.counts.values():
        result.created += counts.created
        result.updated += counts.updated
        result.skipped += counts.skipped
    result.warnings.extend(report.warnings)


def _detail(response: httpx.Response) -> str:
    with contextlib.suppress(Exception):
        body = response.json()
        if isinstance(body, dict) and body.get("detail"):
            return str(body["detail"])
    return ""
