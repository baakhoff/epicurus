"""Notes' half of the tenant portability contract (#872, ADR-0133).

The properties under test are the ones an operator's data depends on: what travels (bodies
included — Postgres is the source of truth, the `.md` mirror is derived output nothing reads
back), what deliberately does not, that a round trip into a *fresh* install reproduces the
tenant exactly, that a second apply changes nothing, and that a dry run counts what an apply
would do without writing a row.

File-backed SQLite under ``tmp_path`` throughout (never in-memory + ``StaticPool``, per
AGENTS.md), and each test's engine is disposed — the round-trip tests hold two databases open
at once, which is exactly the shape a shared in-memory connection cannot serve.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from epicurus_core import EpicurusModule, PortabilityRecord, add_portability_routes
from epicurus_core.files import FileEntry, LocalFileStore
from epicurus_notes.db import NoteFolderStore, NotesStore
from epicurus_notes.mirror import NotesMirror
from epicurus_notes.portability import (
    NOTE,
    NOTE_FOLDER,
    NOTE_SUGGESTION,
    NOTE_SUGGESTION_DECISION,
    SCHEMA,
    NotesPortability,
)
from epicurus_notes.suggestions import NoteSuggestionAuditStore, NoteSuggestionStore

TENANT = "local"
OTHER = "other"

BODY = "# Meeting\n\nDecided: ship it.\n"


async def _open(path: Path) -> AsyncEngine:
    """A fresh, fully-migrated notes database at *path* (all four tables)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    for store in (
        NotesStore(engine),
        NoteFolderStore(engine),
        NoteSuggestionStore(engine),
        NoteSuggestionAuditStore(engine),
    ):
        await store.init()
    return engine


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    created = await _open(tmp_path / "source.db")
    yield created
    await created.dispose()


@pytest.fixture
async def target(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    """A *second*, empty install — the machine the operator is moving into."""
    created = await _open(tmp_path / "target.db")
    yield created
    await created.dispose()


async def _seed(engine: AsyncEngine, tenant: str = TENANT) -> str:
    """Write one of everything through the real stores; returns the suggestion's ``sid``."""
    notes = NotesStore(engine)
    await notes.upsert(tenant=tenant, slug="work/meeting", title="Meeting", content=BODY)
    await notes.upsert(tenant=tenant, slug="idea", title="Idea", content="a thought")
    # Version history exists but must not travel.
    await notes.add_version(tenant=tenant, slug="idea", title="Idea", content="older draft")

    await NoteFolderStore(engine).add(tenant=tenant, path="work")
    await NoteFolderStore(engine).add(tenant=tenant, path="archive")

    suggestions = NoteSuggestionStore(engine)
    staged = await suggestions.add(
        tenant=tenant,
        slug="idea",
        operation="append",
        proposed_content="and another thing",
        origin="agent",
        note="from the chat",
    )
    await NoteSuggestionAuditStore(engine).record(
        tenant=tenant,
        sid="0" * 32,
        slug="work/meeting",
        operation="update",
        origin="agent",
        note="tighten it",
        proposed_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        decision="approved",
        proposed_content="draft",
        applied_content="final",
    )
    return staged.sid


async def _export(engine: AsyncEngine, tenant: str = TENANT) -> list[PortabilityRecord]:
    return [r async for r in NotesPortability(engine).export(tenant_id=tenant)]


def _dump(records: list[PortabilityRecord]) -> list[tuple[str, str, dict[str, Any]]]:
    return [(r.kind, r.id, r.data) for r in records]


async def _stream(records: list[PortabilityRecord]) -> AsyncIterator[PortabilityRecord]:
    for record in records:
        yield record


async def _import(
    engine: AsyncEngine,
    records: list[PortabilityRecord],
    *,
    tenant: str = TENANT,
    dry_run: bool = False,
) -> Any:
    return await NotesPortability(engine).import_(
        tenant_id=tenant, records=_stream(records), dry_run=dry_run
    )


def _of(records: list[PortabilityRecord], kind: str) -> list[PortabilityRecord]:
    return [r for r in records if r.kind == kind]


# ── what travels ─────────────────────────────────────────────────────────────


async def test_export_carries_every_kind_with_a_stable_id(engine: AsyncEngine) -> None:
    sid = await _seed(engine)
    records = await _export(engine)

    assert [r.id for r in _of(records, NOTE)] == ["idea", "work/meeting"]
    assert [r.id for r in _of(records, NOTE_FOLDER)] == ["archive", "work"]
    assert [r.id for r in _of(records, NOTE_SUGGESTION)] == [sid]
    # A decision has no natural id of its own: "<sid>:<decided_at ISO-8601 UTC>".
    (decision,) = _of(records, NOTE_SUGGESTION_DECISION)
    prefix, _, decided_at = decision.id.partition(":")
    assert prefix == "0" * 32
    assert datetime.fromisoformat(decided_at).tzinfo is not None


async def test_export_carries_the_note_body(engine: AsyncEngine) -> None:
    """The body is the *point*. Postgres owns it; the `.md` mirror is write-only derived
    output that no import step ever reads back, so a metadata-only export would land the
    operator a Files tree full of markdown and a Notes page full of empty documents."""
    await _seed(engine)
    (_, note) = sorted(_of(await _export(engine), NOTE), key=lambda r: r.id)

    assert note.id == "work/meeting"
    assert note.data["content"] == BODY
    assert note.data["title"] == "Meeting"


async def test_export_omits_tenant_and_the_surrogate_key(engine: AsyncEngine) -> None:
    await _seed(engine)
    records = await _export(engine)

    for record in records:
        assert "tenant" not in record.data
        assert "id" not in record.data
    # Nor anywhere in the serialised stream (the target tenant re-applies its own).
    blob = json.dumps(_dump(records))
    assert TENANT not in blob


async def test_export_omits_version_history(engine: AsyncEngine) -> None:
    """``note_versions`` is derived from edits, capped and deduped, and every row is a whole
    body — the head of that history is the note, which travels in full."""
    await _seed(engine)
    records = await _export(engine)

    assert {r.kind for r in records} == {
        NOTE,
        NOTE_FOLDER,
        NOTE_SUGGESTION,
        NOTE_SUGGESTION_DECISION,
    }
    assert "older draft" not in json.dumps(_dump(records))


async def test_export_is_tenant_scoped(engine: AsyncEngine) -> None:
    await _seed(engine, tenant=TENANT)
    await _seed(engine, tenant=OTHER)

    ours = await _export(engine, tenant=TENANT)
    theirs = await _export(engine, tenant=OTHER)

    assert len(ours) == len(theirs) == 6
    # Same slugs, different rows — and neither export can see the other's suggestion.
    assert {r.id for r in _of(ours, NOTE_SUGGESTION)}.isdisjoint(
        {r.id for r in _of(theirs, NOTE_SUGGESTION)}
    )


# ── round trip ───────────────────────────────────────────────────────────────


async def test_round_trip_into_a_fresh_install_reproduces_the_tenant(
    engine: AsyncEngine, target: AsyncEngine
) -> None:
    await _seed(engine)
    exported = await _export(engine)

    report = await _import(target, exported)

    assert report.schema_name == SCHEMA
    assert report.warnings == []
    assert report.total == len(exported)
    assert all(counts.created == counts.total for counts in report.counts.values())
    assert _dump(await _export(target)) == _dump(exported)


async def test_round_trip_preserves_the_body_and_its_timestamps(
    engine: AsyncEngine, target: AsyncEngine
) -> None:
    await _seed(engine)
    await _import(target, await _export(engine))

    note = await NotesStore(target).get(tenant=TENANT, slug="work/meeting")
    assert note is not None
    assert note.content == BODY
    source = await NotesStore(engine).get(tenant=TENANT, slug="work/meeting")
    assert source is not None
    assert note.updated_at.replace(tzinfo=UTC) == source.updated_at.replace(tzinfo=UTC)


async def test_import_lands_under_the_target_tenant(
    engine: AsyncEngine, target: AsyncEngine
) -> None:
    """``tenant`` never travels: the archive is applied under whatever tenant is named."""
    await _seed(engine, tenant=TENANT)
    await _import(target, await _export(engine, tenant=TENANT), tenant=OTHER)

    assert await NotesStore(target).count(tenant=OTHER) == 2
    assert await NotesStore(target).count(tenant=TENANT) == 0


async def test_second_apply_is_a_no_op(engine: AsyncEngine, target: AsyncEngine) -> None:
    await _seed(engine)
    exported = await _export(engine)
    await _import(target, exported)

    again = await _import(target, exported)

    assert again.total == len(exported)
    assert all(counts.skipped == counts.total for counts in again.counts.values())
    assert _dump(await _export(target)) == _dump(exported)


async def test_import_updates_a_changed_row_and_never_deletes(
    engine: AsyncEngine, target: AsyncEngine
) -> None:
    await _seed(engine)
    exported = await _export(engine)
    await _import(target, exported)
    # The target has moved on: one note edited, one note only it knows about.
    notes = NotesStore(target)
    await notes.upsert(tenant=TENANT, slug="idea", title="Idea", content="edited here")
    await notes.upsert(tenant=TENANT, slug="local-only", title="Local", content="mine")

    report = await _import(target, exported)

    assert report.counts[NOTE].updated == 1
    assert report.counts[NOTE].skipped == 1
    restored = await notes.get(tenant=TENANT, slug="idea")
    assert restored is not None and restored.content == "a thought"
    # Nothing this contract can do deletes: the target's own note is untouched.
    survivor = await notes.get(tenant=TENANT, slug="local-only")
    assert survivor is not None and survivor.content == "mine"


# ── dry run, unknown kinds, malformed records ────────────────────────────────


async def test_dry_run_counts_without_writing(engine: AsyncEngine, target: AsyncEngine) -> None:
    await _seed(engine)
    exported = await _export(engine)

    preview = await _import(target, exported, dry_run=True)

    assert preview.total == len(exported)
    assert all(counts.created == counts.total for counts in preview.counts.values())
    assert await _export(target) == []
    # ...and applying for real then reports exactly what the preview promised.
    applied = await _import(target, exported)
    assert applied.counts == preview.counts


async def test_dry_run_counts_a_second_apply_as_skipped(
    engine: AsyncEngine, target: AsyncEngine
) -> None:
    await _seed(engine)
    exported = await _export(engine)
    await _import(target, exported)

    preview = await _import(target, exported, dry_run=True)

    assert all(counts.skipped == counts.total for counts in preview.counts.values())


async def test_unknown_kind_is_skipped_with_a_warning(target: AsyncEngine) -> None:
    report = await _import(
        target,
        [
            PortabilityRecord(kind="note_telepathy", id="x", data={"body": "?"}),
            PortabilityRecord(kind="note_telepathy", id="y", data={"body": "?"}),
        ],
    )

    assert report.counts["note_telepathy"].skipped == 2
    # De-duplicated: one complaint, not one per record.
    assert len(report.warnings) == 1
    assert "note_telepathy" in report.warnings[0]


async def test_malformed_record_is_skipped_not_fatal(target: AsyncEngine) -> None:
    """One unreadable line must not cost the operator the other 9,999."""
    report = await _import(
        target,
        [
            # A decision id that doesn't name the (sid, decided_at) pair it must.
            PortabilityRecord(kind=NOTE_SUGGESTION_DECISION, id="nocolon", data={}),
            PortabilityRecord(kind=NOTE, id="ok", data={"title": "T", "content": "c"}),
            PortabilityRecord(
                kind=NOTE, id="bad-date", data={"title": "T", "content": "c", "created_at": "soon"}
            ),
        ],
    )

    assert report.counts[NOTE].created == 1
    assert report.counts[NOTE].skipped == 1
    assert report.counts[NOTE_SUGGESTION_DECISION].skipped == 1
    assert len(report.warnings) == 2
    assert await NotesStore(target).count(tenant=TENANT) == 1


# ── the body-before-the-mirror question (#872) ───────────────────────────────


async def test_a_note_arrives_whole_before_any_file_is_restored(target: AsyncEngine) -> None:
    """The core applies modules *before* ``files/`` and before the forced rescan, so a note's
    row lands while its `.md` mirror does not yet exist anywhere. Because the row carries its
    own body, that costs nothing: the note is readable the moment the import returns."""
    await _import(
        target,
        [PortabilityRecord(kind=NOTE, id="fresh", data={"title": "Fresh", "content": BODY})],
    )

    note = await NotesStore(target).get(tenant=TENANT, slug="fresh")
    assert note is not None and note.content == BODY


async def test_the_startup_backfill_rebuilds_a_missing_mirror(
    target: AsyncEngine, tmp_path: Path
) -> None:
    """The other half of the same answer: whatever the archive's ``files/`` member did or did
    not carry, the module's own startup backfill writes a `.md` for every note without one."""
    await _import(
        target,
        [PortabilityRecord(kind=NOTE, id="fresh", data={"title": "Fresh", "content": BODY})],
    )
    files_root = tmp_path / "files"
    notes_root = files_root / TENANT / "notes"
    mirror = NotesMirror(
        notes_root,
        NotesStore(target),
        tenant=TENANT,
        platform=_FakePlatform(files_root),  # type: ignore[arg-type]
        core_prefix="notes",
    )

    assert await mirror.backfill() == 1
    assert (notes_root / "fresh.md").read_text(encoding="utf-8") == BODY


async def test_a_record_without_a_body_never_creates_a_phantom_note(target: AsyncEngine) -> None:
    """A metadata-only record cannot conjure an empty note into the editor..."""
    report = await _import(
        target, [PortabilityRecord(kind=NOTE, id="ghost", data={"title": "Ghost"})]
    )

    assert report.counts[NOTE].skipped == 1
    assert any("content" in w for w in report.warnings)
    assert await NotesStore(target).get(tenant=TENANT, slug="ghost") is None


async def test_a_record_without_a_body_never_blanks_an_existing_note(
    target: AsyncEngine,
) -> None:
    """...nor empty one that is already there. An absent key leaves its column alone."""
    await NotesStore(target).upsert(tenant=TENANT, slug="kept", title="Kept", content=BODY)

    report = await _import(
        target, [PortabilityRecord(kind=NOTE, id="kept", data={"title": "Renamed"})]
    )

    assert report.counts[NOTE].updated == 1
    note = await NotesStore(target).get(tenant=TENANT, slug="kept")
    assert note is not None
    assert note.content == BODY
    assert note.title == "Renamed"


# ── the routes (the contract as the core actually calls it) ──────────────────


def _app(engine: AsyncEngine) -> FastAPI:
    app = FastAPI()
    module = EpicurusModule("notes", version="0.14.0", portable=True)
    add_portability_routes(app, module, NotesPortability(engine))
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://notes")


def _ndjson(schema: str, records: list[PortabilityRecord]) -> bytes:
    lines = [json.dumps({"schema": schema, "component_version": "0.14.0"})]
    lines += [json.dumps(r.model_dump()) for r in records]
    return ("\n".join(lines) + "\n").encode()


async def test_route_export_streams_a_header_then_records(engine: AsyncEngine) -> None:
    await _seed(engine)

    async with _client(_app(engine)) as client:
        response = await client.get("/export", params={"tenant_id": TENANT})

    assert response.status_code == 200
    header, *rest = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert header["schema"] == SCHEMA
    assert header["component_version"] == "0.14.0"
    assert len(rest) == 6


async def test_route_export_requires_a_tenant(engine: AsyncEngine) -> None:
    async with _client(_app(engine)) as client:
        assert (await client.get("/export")).status_code == 400


async def test_route_import_accepts_an_older_schema_with_a_warning(
    engine: AsyncEngine, target: AsyncEngine
) -> None:
    await _seed(engine)
    body = _ndjson("notes/0", await _export(engine))

    async with _client(_app(target)) as client:
        response = await client.post("/import", params={"tenant_id": TENANT}, content=body)

    assert response.status_code == 200
    assert any("older schema" in w for w in response.json()["warnings"])
    assert await NotesStore(target).count(tenant=TENANT) == 2


async def test_route_import_refuses_a_newer_schema(target: AsyncEngine) -> None:
    body = _ndjson("notes/99", [PortabilityRecord(kind=NOTE, id="x", data={"content": "c"})])

    async with _client(_app(target)) as client:
        response = await client.post("/import", params={"tenant_id": TENANT}, content=body)

    assert response.status_code == 409
    assert await NotesStore(target).count(tenant=TENANT) == 0


async def test_route_import_refuses_a_foreign_module(target: AsyncEngine) -> None:
    body = _ndjson("tasks/1", [PortabilityRecord(kind=NOTE, id="x", data={"content": "c"})])

    async with _client(_app(target)) as client:
        response = await client.post("/import", params={"tenant_id": TENANT}, content=body)

    assert response.status_code == 409


async def test_route_import_dry_run_writes_nothing(
    engine: AsyncEngine, target: AsyncEngine
) -> None:
    await _seed(engine)
    body = _ndjson(SCHEMA, await _export(engine))

    async with _client(_app(target)) as client:
        response = await client.post(
            "/import", params={"tenant_id": TENANT, "dry_run": "true"}, content=body
        )

    assert response.status_code == 200
    assert response.json()["counts"][NOTE]["created"] == 2
    assert await NotesStore(target).count(tenant=TENANT) == 0


class _FakePlatform:
    """A stand-in ``PlatformClient`` whose ``files_*`` calls hit a real ``LocalFileStore``.

    Inlined per the repo convention (no ``tests/__init__.py``) — the same shape
    ``test_mirror.py`` uses, so the backfill's writes land where the core would put them.
    """

    def __init__(self, files_root: Path) -> None:
        self.store = LocalFileStore(files_root)

    async def files_write(self, path: str, content: str) -> FileEntry:
        return await self.store.write_text(tenant=TENANT, path=path, content=content)

    async def files_delete(self, path: str) -> bool:
        return await self.store.delete(tenant=TENANT, path=path)

    async def files_stat(self, path: str) -> FileEntry | None:
        return await self.store.stat(tenant=TENANT, path=path)
