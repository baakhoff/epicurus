"""Calendar's half of the tenant export/import contract (#870, part of #866).

The contract's promises are properties, not endpoints, so they are tested as properties: a
round trip through a *second, empty* database must reproduce the calendar exactly — a
recurring series with an edited occurrence and a tombstoned one included, as expanded by the
provider rather than merely as rows — a second apply must change nothing, a dry run must
write nothing, and one tenant's export must never contain another's events.

Every engine here is **file-backed** SQLite under ``tmp_path`` with default pooling, never
in-memory + ``StaticPool``: the round-trip tests hold two engines open at once and the
streaming export reads through its own connection while the import writes through another.
Each is disposed in teardown so aiosqlite's worker threads stop before the loop closes.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from epicurus_calendar.db import LocalEventStore, _StoredEvent, instance_id
from epicurus_calendar.lead_time_prefs import LeadTimePrefsStore
from epicurus_calendar.models import Attendee, DateTimeRange, Event
from epicurus_calendar.portability import (
    CALENDAR_SCHEMA,
    EVENT_RECORD_KIND,
    LEAD_TIME_RECORD_KIND,
    CalendarPortability,
)
from epicurus_calendar.providers.local import LocalCalendarProvider
from epicurus_calendar.service import build_module
from epicurus_core import ImportReport, PortabilityRecord, add_portability_routes
from epicurus_core.db import ensure_columns

TENANT = "local"
OTHER_TENANT = "other"

# The window every expansion assertion reads through — wide enough to cover the whole
# fixture series, narrow enough that a stray event would be noticed.
WINDOW = DateTimeRange(
    start=datetime(2026, 7, 1, tzinfo=UTC),
    end=datetime(2026, 8, 15, tzinfo=UTC),
)


def _dt(day: int, hour: int = 9) -> datetime:
    return datetime(2026, 7, day, hour, 0, tzinfo=UTC)


# ── fixtures ──────────────────────────────────────────────────────────────────


class _Side:
    """One installation: an engine, the two stores that own travelling tables, a provider."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.events = LocalEventStore(engine)
        self.lead_prefs = LeadTimePrefsStore(engine)
        self.provider = LocalCalendarProvider(store=self.events)
        self.portability = CalendarPortability(engine)

    async def init(self) -> None:
        await self.events.init()
        await self.lead_prefs.init()


async def _side(path: Path) -> _Side:
    side = _Side(create_async_engine(f"sqlite+aiosqlite:///{path}"))
    await side.init()
    return side


@pytest.fixture
async def source(tmp_path: Path) -> AsyncIterator[_Side]:
    side = await _side(tmp_path / "calendar-source.db")
    yield side
    await side.engine.dispose()


@pytest.fixture
async def target(tmp_path: Path) -> AsyncIterator[_Side]:
    side = await _side(tmp_path / "calendar-target.db")
    yield side
    await side.engine.dispose()


async def _populate(side: _Side, *, tenant: str = TENANT) -> None:
    """A calendar with every row shape the local store can hold (#432).

    A plain timed event, an all-day event, and a weekly series carrying both kinds of
    exception — one occurrence moved and retitled, one deleted outright. The series is
    built through the *provider*, which is the only thing that writes an exception in
    production, so the rows under test are the rows the product actually creates.
    """
    await side.events.create_event(
        tenant=tenant,
        title="Standup",
        start=_dt(2),
        end=_dt(2, 10),
        description="Daily sync",
        location="Room 3",
        attendees=[
            Attendee(email="ada@example.com", display_name="Ada", response_status="accepted")
        ],
    )
    await side.events.create_event(
        tenant=tenant,
        title="Conference",
        start=datetime(2026, 7, 20, tzinfo=UTC),
        end=datetime(2026, 7, 23, tzinfo=UTC),
        all_day=True,
    )
    series = await side.events.create_event(
        tenant=tenant,
        title="Weekly review",
        start=_dt(6, 14),
        end=_dt(6, 15),
        recurrence="FREQ=WEEKLY;COUNT=5",
        timezone="Europe/Berlin",
    )
    # An edited occurrence: moved an hour later and retitled (exception row).
    moved = await side.provider.update_event(
        tenant_id=tenant,
        event_id=instance_id(series.id, _dt(13, 14)),
        title="Weekly review (long)",
        start=_dt(13, 15),
        end=_dt(13, 17),
        edit_scope="this",
    )
    assert moved is not None
    # A deleted occurrence: a tombstone exception, not a row removal.
    assert await side.provider.delete_event(
        tenant_id=tenant,
        event_id=instance_id(series.id, _dt(20, 14)),
        edit_scope="this",
    )
    await side.lead_prefs.set_lead_minutes(tenant, 45)


async def _records(side: _Side, *, tenant: str = TENANT) -> list[PortabilityRecord]:
    return [record async for record in side.portability.export(tenant_id=tenant)]


async def _replay(records: list[PortabilityRecord]) -> AsyncIterator[PortabilityRecord]:
    for record in records:
        yield record


async def _apply(
    side: _Side,
    records: list[PortabilityRecord],
    *,
    tenant: str = TENANT,
    dry_run: bool = False,
) -> ImportReport:
    return await side.portability.import_(
        tenant_id=tenant, records=_replay(records), dry_run=dry_run
    )


async def _expanded(side: _Side, *, tenant: str = TENANT) -> list[Event]:
    events = await side.provider.list_events(tenant_id=tenant, time_range=WINDOW)
    return sorted(events, key=lambda e: (e.start, e.id))


# ── the export stream ─────────────────────────────────────────────────────────


async def test_export_yields_events_and_the_lead_time_preference(source: _Side) -> None:
    await _populate(source)
    records = await _records(source)
    kinds = {record.kind for record in records}
    assert kinds == {EVENT_RECORD_KIND, LEAD_TIME_RECORD_KIND}
    # Three created rows plus the two exception rows the provider wrote.
    assert sum(r.kind == EVENT_RECORD_KIND for r in records) == 5
    lead = next(r for r in records if r.kind == LEAD_TIME_RECORD_KIND)
    assert lead.id == LEAD_TIME_RECORD_KIND  # a singleton: the tenant is the key
    assert lead.data == {"lead_minutes": 45}


async def test_export_strips_the_tenant_and_the_surrogate_key(source: _Side) -> None:
    await _populate(source)
    for record in await _records(source):
        assert "tenant" not in record.data, "the tenant is context, never archive content"
        assert "id" not in record.data, "the autoincrement pk means nothing elsewhere"


async def test_event_record_id_is_the_stable_event_id(source: _Side) -> None:
    await _populate(source)
    events = [r for r in await _records(source) if r.kind == EVENT_RECORD_KIND]
    assert all(record.id == record.data["event_id"] for record in events)
    # The exceptions' ids are instance ids (``<series>_<original start>``), so a series and
    # its overrides stay linked across installations without a surrogate anywhere.
    exceptions = [r for r in events if r.data["recurring_event_id"] is not None]
    assert len(exceptions) == 2
    assert all("_" in record.id for record in exceptions)


async def test_export_records_are_json_serialisable(source: _Side) -> None:
    # The stream is NDJSON — a datetime that never became a string would break the archive.
    await _populate(source)
    for record in await _records(source):
        json.loads(json.dumps(record.data))


async def test_export_is_tenant_isolated(source: _Side) -> None:
    await _populate(source, tenant=TENANT)
    await _populate(source, tenant=OTHER_TENANT)
    ours = await _records(source, tenant=TENANT)
    theirs = await _records(source, tenant=OTHER_TENANT)
    assert {r.id for r in ours}.isdisjoint({r.id for r in theirs if r.kind == EVENT_RECORD_KIND})
    assert len(ours) == len(theirs)


async def test_export_of_an_empty_tenant_is_empty(source: _Side) -> None:
    await _populate(source, tenant=OTHER_TENANT)
    assert await _records(source, tenant=TENANT) == []


# ── the round trip ────────────────────────────────────────────────────────────


async def test_round_trip_reproduces_every_row(source: _Side, target: _Side) -> None:
    await _populate(source)
    report = await _apply(target, await _records(source))
    assert report.counts[EVENT_RECORD_KIND].created == 5
    assert report.counts[LEAD_TIME_RECORD_KIND].created == 1
    assert report.warnings == []
    assert await _records(target) == await _records(source)


async def test_round_trip_preserves_the_expanded_series(source: _Side, target: _Side) -> None:
    """The real assertion: what the *provider* returns must be identical on both sides.

    Rows matching is necessary but not sufficient — a series is only correct if its RRULE,
    its anchor timezone, its moved occurrence and its tombstone all survive together, and
    the only way to see that is to expand it.
    """
    await _populate(source)
    await _apply(target, await _records(source))
    before, after = await _expanded(source), await _expanded(target)
    assert [e.model_dump() for e in after] == [e.model_dump() for e in before]
    titles = [e.title for e in after]
    assert titles.count("Weekly review (long)") == 1  # the edited occurrence
    assert "Weekly review" in titles
    # COUNT=5 minus the deleted occurrence, plus the two standalone events.
    assert len(after) == 6


async def test_round_trip_preserves_the_lead_time_preference(source: _Side, target: _Side) -> None:
    await _populate(source)
    await _apply(target, await _records(source))
    assert await target.lead_prefs.get_lead_minutes(TENANT) == 45


async def test_import_re_tenants_the_records(source: _Side, target: _Side) -> None:
    # The archive carries no tenant, so the same stream lands wherever it is told to.
    await _populate(source)
    await _apply(target, await _records(source), tenant=OTHER_TENANT)
    assert await _records(target, tenant=TENANT) == []
    assert len(await _records(target, tenant=OTHER_TENANT)) == 6


# ── idempotence ───────────────────────────────────────────────────────────────


async def test_second_apply_is_a_no_op(source: _Side, target: _Side) -> None:
    await _populate(source)
    records = await _records(source)
    await _apply(target, records)
    again = await _apply(target, records)
    assert again.counts[EVENT_RECORD_KIND].skipped == 5
    assert again.counts[EVENT_RECORD_KIND].created == 0
    assert again.counts[EVENT_RECORD_KIND].updated == 0
    assert again.counts[LEAD_TIME_RECORD_KIND].skipped == 1
    assert len(await _records(target)) == 6, "nothing was duplicated"


async def test_import_into_the_source_itself_changes_nothing(source: _Side) -> None:
    # Importing an archive back where it came from is the sharpest idempotence case: every
    # value round-trips through the encoder and must compare equal to what is already there.
    await _populate(source)
    records = await _records(source)
    report = await _apply(source, records)
    assert report.counts[EVENT_RECORD_KIND].skipped == 5
    assert await _records(source) == records


async def test_import_updates_a_changed_row_and_never_deletes(source: _Side, target: _Side) -> None:
    await _populate(source)
    records = await _records(source)
    await _apply(target, records)
    # The target edits one event and adds one of its own; re-applying must overwrite the
    # first and leave the second standing (a merge, not a replace).
    standup = next(r for r in records if r.data["title"] == "Standup")
    await target.events.update_event(
        tenant=TENANT, event_id=standup.data["event_id"], title="Standup (moved)"
    )
    await target.events.create_event(
        tenant=TENANT, title="Target-only", start=_dt(3), end=_dt(3, 10)
    )
    report = await _apply(target, records)
    assert report.counts[EVENT_RECORD_KIND].updated == 1
    assert report.counts[EVENT_RECORD_KIND].skipped == 4
    titles = {e.title for e in await _expanded(target)}
    assert "Standup" in titles and "Target-only" in titles


# ── dry run ───────────────────────────────────────────────────────────────────


async def test_dry_run_counts_exactly_what_apply_would_do(source: _Side, target: _Side) -> None:
    await _populate(source)
    records = await _records(source)
    preview = await _apply(target, records, dry_run=True)
    assert await _records(target) == [], "a dry run must not write"
    applied = await _apply(target, records)
    assert preview.counts == applied.counts


async def test_dry_run_over_a_populated_target_writes_nothing(source: _Side, target: _Side) -> None:
    await _populate(source)
    records = await _records(source)
    await _apply(target, records)
    before = await _records(target)
    standup = next(r for r in records if r.data["title"] == "Standup")
    await target.events.update_event(
        tenant=TENANT, event_id=standup.data["event_id"], title="Standup (moved)"
    )
    preview = await _apply(target, records, dry_run=True)
    assert preview.counts[EVENT_RECORD_KIND].updated == 1
    after = await _records(target)
    assert after != before  # the local edit is still there…
    assert next(r for r in after if r.id == standup.id).data["title"] == "Standup (moved)"


# ── unknown records ───────────────────────────────────────────────────────────


async def test_unknown_kind_is_skipped_with_a_warning(target: _Side) -> None:
    report = await _apply(
        target, [PortabilityRecord(kind="reminder", id="r1", data={"note": "hi"})]
    )
    assert report.counts["reminder"].skipped == 1
    assert any("reminder" in warning for warning in report.warnings)
    assert await _records(target) == []


async def test_unknown_field_is_ignored_with_a_warning(source: _Side, target: _Side) -> None:
    # A newer calendar exported a column this version has no place for: the rest of the row
    # still lands, and the operator is told what was dropped.
    await _populate(source)
    records = await _records(source)
    event = next(r for r in records if r.kind == EVENT_RECORD_KIND)
    event.data["colour"] = "tangerine"
    report = await _apply(target, records)
    assert report.counts[EVENT_RECORD_KIND].created == 5
    assert any("colour" in warning for warning in report.warnings)


# ── the routes ────────────────────────────────────────────────────────────────


def _app(side: _Side) -> FastAPI:
    module = build_module(side.provider, tenant_id=TENANT)
    app = FastAPI()
    add_portability_routes(app, module, side.portability)
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://calendar")


def _lines(body: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in body.splitlines() if line.strip()]


async def test_manifest_declares_portable(source: _Side) -> None:
    module = build_module(source.provider, tenant_id=TENANT)
    assert (await module.manifest()).portable is True


async def test_export_route_streams_a_header_then_records(source: _Side) -> None:
    await _populate(source)
    module = build_module(source.provider, tenant_id=TENANT)
    async with _client(_app(source)) as client:
        response = await client.get("/export", params={"tenant_id": TENANT})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    lines = _lines(response.text)
    assert lines[0]["schema"] == CALENDAR_SCHEMA
    # The module's release version travels beside the record schema — compared to the
    # manifest rather than pinned, so a bump does not need an edit here.
    assert lines[0]["component_version"] == (await module.manifest()).version
    assert len(lines) == 7  # header + 5 events + the lead-time preference


async def test_export_route_requires_a_tenant(source: _Side) -> None:
    async with _client(_app(source)) as client:
        assert (await client.get("/export")).status_code == 400


async def test_import_route_round_trips_through_the_wire(source: _Side, target: _Side) -> None:
    await _populate(source)
    async with _client(_app(source)) as client:
        exported = (await client.get("/export", params={"tenant_id": TENANT})).text
    async with _client(_app(target)) as client:
        response = await client.post(
            "/import", params={"tenant_id": TENANT}, content=exported.encode("utf-8")
        )
    assert response.status_code == 200
    assert response.json()["counts"][EVENT_RECORD_KIND]["created"] == 5
    assert [e.model_dump() for e in await _expanded(target)] == [
        e.model_dump() for e in await _expanded(source)
    ]


async def test_import_route_accepts_an_older_schema_with_a_warning(target: _Side) -> None:
    stream = json.dumps({"schema": "calendar/0", "component_version": "0.20.1"}) + "\n"
    async with _client(_app(target)) as client:
        response = await client.post(
            "/import", params={"tenant_id": TENANT}, content=stream.encode("utf-8")
        )
    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == CALENDAR_SCHEMA
    assert any("older schema" in warning for warning in body["warnings"])


async def test_import_route_refuses_a_newer_schema(source: _Side, target: _Side) -> None:
    await _populate(source)
    records = await _records(source)
    stream = json.dumps({"schema": "calendar/2", "component_version": "9.9.9"}) + "\n"
    stream += "".join(json.dumps(record.model_dump(), default=str) + "\n" for record in records)
    async with _client(_app(target)) as client:
        response = await client.post(
            "/import", params={"tenant_id": TENANT}, content=stream.encode("utf-8")
        )
    assert response.status_code == 409
    assert await _records(target) == [], "a refusal must never be half-applied"


async def test_import_route_refuses_a_foreign_module(target: _Side) -> None:
    stream = json.dumps({"schema": "tasks/1", "component_version": "0.23.3"}) + "\n"
    async with _client(_app(target)) as client:
        response = await client.post(
            "/import", params={"tenant_id": TENANT}, content=stream.encode("utf-8")
        )
    assert response.status_code == 409


async def test_import_route_is_tenant_scoped(source: _Side, target: _Side) -> None:
    await _populate(source)
    async with _client(_app(source)) as client:
        exported = (await client.get("/export", params={"tenant_id": TENANT})).text
    async with _client(_app(target)) as client:
        await client.post(
            "/import", params={"tenant_id": OTHER_TENANT}, content=exported.encode("utf-8")
        )
    assert await _records(target, tenant=TENANT) == []
    assert len(await _records(target, tenant=OTHER_TENANT)) == 6


async def test_lead_time_default_is_not_exported_when_unset(source: _Side) -> None:
    # An operator who never touched the setting has no row; the archive must not invent one
    # (importing it elsewhere would pin that install to today's default forever).
    await source.events.create_event(tenant=TENANT, title="Solo", start=_dt(4), end=_dt(4, 10))
    records = await _records(source)
    assert all(record.kind == EVENT_RECORD_KIND for record in records)


async def test_attendees_and_all_day_survive_the_trip(source: _Side, target: _Side) -> None:
    await _populate(source)
    await _apply(target, await _records(source))
    events = {e.title: e for e in await _expanded(target)}
    standup = events["Standup"]
    assert [a.email for a in standup.attendees] == ["ada@example.com"]
    assert standup.attendees[0].display_name == "Ada"
    assert standup.attendees[0].response_status == "accepted"
    conference = events["Conference"]
    assert conference.all_day is True
    assert conference.end - conference.start == timedelta(days=3)


# ── a NULL a reconciled source carries (#903) ─────────────────────────────────


async def _reconciled_source(path: Path) -> _Side:
    """A calendar the way a long-lived, not-yet-fully-migrated install actually is:
    ``all_day``/``excluded`` nullable.

    Not hand-carved DDL pretending to be old — the real path, minus its final step. Before
    #834/#928, ``calendar_events`` was created with only the columns of its *first* release and
    a row in it, then :meth:`LocalEventStore.init` ran the shared additive reconcile (#249,
    ADR-0067) to add the rest. That reconcile now lives in the migration baseline's
    ``ep.create_table`` (:mod:`epicurus_core.db.ops`) instead of in ``init()`` — called here
    directly, the very code the baseline calls, to reproduce the exact intermediate state a
    real upgrade passes through: revision 0001 has reconciled the table but 0002 (which
    backfills these two columns and adds their server default) has not run yet. Neither boolean
    has a ``server_default`` at 0001, so there is nothing to backfill a populated table with and
    both are added **nullable** — leaving the pre-existing row with ``NULL`` in a column the
    model declares ``NOT NULL``. ``_row_to_event`` coerces that to ``False`` on every ordinary
    read, which is exactly why it went unnoticed until portability inserted the value verbatim
    into a fresh schema and the module 500'd (#903).
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE calendar_events ("
            " id INTEGER NOT NULL PRIMARY KEY,"
            " tenant VARCHAR(63) NOT NULL,"
            " event_id VARCHAR(64) NOT NULL,"
            " title VARCHAR(512) NOT NULL,"
            " start_dt DATETIME NOT NULL,"
            " end_dt DATETIME NOT NULL,"
            " description TEXT,"
            " location VARCHAR(512),"
            " created_at DATETIME NOT NULL,"
            " CONSTRAINT uq_calendar_tenant_event UNIQUE (tenant, event_id))"
        )
        await conn.exec_driver_sql(
            "INSERT INTO calendar_events"
            " (tenant, event_id, title, start_dt, end_dt, created_at)"
            " VALUES ('local', 'old-1', 'Retrospective',"
            " '2026-07-08 09:00:00.000000', '2026-07-08 10:00:00.000000',"
            " '2026-01-01 00:00:00.000000')"
        )
        # What the baseline's reconcile arm does to a table it finds already present — the
        # same helper, called over the full model column list, the way ``ep.create_table``
        # calls it. This is 0001's half of the upgrade; 0002 (not run here) is what fixes it.
        await conn.run_sync(
            lambda sync_conn: ensure_columns(
                sync_conn,
                _StoredEvent.__table__,
                [c.name for c in _StoredEvent.__table__.columns],
            )
        )
    side = _Side(engine)
    # events.init() is now a plain create_all — a no-op against the table just built above —
    # so this only creates the lead-time-prefs table the export below also reads.
    await side.init()
    return side


async def test_a_reconciled_source_really_does_hold_a_null_in_a_not_null_column(
    tmp_path: Path,
) -> None:
    """The premise of the whole fix, asserted rather than assumed."""
    side = await _reconciled_source(tmp_path / "legacy.db")
    try:
        assert _StoredEvent.__table__.c.all_day.nullable is False  # the *model* says NOT NULL
        async with side.engine.connect() as conn:
            row = (
                await conn.exec_driver_sql("SELECT all_day, excluded FROM calendar_events")
            ).one()
        assert row.all_day is None  # the *database* holds NULL anyway
        assert row.excluded is None
    finally:
        await side.engine.dispose()


async def test_a_reconciled_calendar_imports_into_a_fresh_schema(
    tmp_path: Path, target: _Side
) -> None:
    """The #903 reproduction, end to end: this used to be a 500 and a lost calendar.

    The export normalises on the way out and the import normalises on the way in, so the
    values that reach the table are the model's defaults — asserted on the row itself, not
    merely on the absence of an exception.
    """
    side = await _reconciled_source(tmp_path / "legacy.db")
    try:
        records = await _records(side)
        assert records[0].data["all_day"] is False  # normalised on the way out
        assert records[0].data["excluded"] is False
        report = await _apply(target, records)
    finally:
        await side.engine.dispose()

    assert report.counts[EVENT_RECORD_KIND].created == 1
    assert report.warnings == []
    async with target.engine.connect() as conn:
        row = (
            await conn.exec_driver_sql("SELECT all_day, excluded, title FROM calendar_events")
        ).one()
    assert row.title == "Retrospective"
    assert row.all_day == 0  # a real value, never the NULL the source carried
    assert row.excluded == 0


async def test_an_archive_written_before_the_fix_still_applies(target: _Side) -> None:
    """The reader normalises too — otherwise every archive already on disk stays broken."""
    report = await _apply(
        target,
        [
            PortabilityRecord(
                kind=EVENT_RECORD_KIND,
                id="old-1",
                data={
                    "event_id": "old-1",
                    "title": "Retrospective",
                    "start_dt": "2026-07-08T09:00:00+00:00",
                    "end_dt": "2026-07-08T10:00:00+00:00",
                    "description": None,
                    "location": None,
                    "all_day": None,
                    "recurrence": None,
                    "recurring_event_id": None,
                    "excluded": None,
                    "attendees": None,
                    "timezone": None,
                    "created_at": "2026-01-01T00:00:00+00:00",
                },
            )
        ],
    )
    assert report.counts[EVENT_RECORD_KIND].created == 1
    events = await _expanded(target)
    assert [e.title for e in events] == ["Retrospective"]
    assert events[0].all_day is False


async def test_re_applying_a_pre_fix_archive_is_still_a_no_op(source: _Side, target: _Side) -> None:
    """A `null` and the default it stands for are the same value, so the second apply skips."""
    await source.events.create_event(tenant=TENANT, title="Solo", start=_dt(4), end=_dt(4, 10))
    records = await _records(source)
    # An archive from before the export normalised: the booleans travel as `null`.
    stale = [
        PortabilityRecord(
            kind=record.kind,
            id=record.id,
            data={**record.data, "all_day": None, "excluded": None},
        )
        for record in records
    ]
    first = await _apply(target, stale)
    second = await _apply(target, stale)
    assert first.counts[EVENT_RECORD_KIND].created == 1
    assert second.counts[EVENT_RECORD_KIND].skipped == 1
    assert second.counts[EVENT_RECORD_KIND].updated == 0


async def test_a_null_that_cannot_be_defaulted_costs_one_event_not_the_calendar(
    source: _Side, target: _Side
) -> None:
    """One unusable row must not take the rest of the stream — it all runs in one transaction."""
    await source.events.create_event(tenant=TENANT, title="Solo", start=_dt(4), end=_dt(4, 10))
    good = await _records(source)
    broken = PortabilityRecord(
        kind=EVENT_RECORD_KIND,
        id="broken-1",
        # `title` is NOT NULL with no default of any kind: there is nothing to fill it with.
        data={**good[0].data, "event_id": "broken-1", "title": None},
    )
    report = await _apply(target, [broken, *good])

    assert report.counts[EVENT_RECORD_KIND].skipped == 1
    assert report.counts[EVENT_RECORD_KIND].created == 1
    assert any("title" in warning for warning in report.warnings)
    assert [event.title for event in await _expanded(target)] == ["Solo"]


async def test_a_nullable_columns_null_is_left_alone(source: _Side, target: _Side) -> None:
    """A NULL that means something is data — `recurrence` on a plain event, and it stays NULL."""
    await source.events.create_event(tenant=TENANT, title="Solo", start=_dt(4), end=_dt(4, 10))
    records = await _records(source)
    assert records[0].data["recurrence"] is None
    assert records[0].data["timezone"] is None
    await _apply(target, records)
    async with target.engine.connect() as conn:
        row = (await conn.exec_driver_sql("SELECT recurrence, timezone FROM calendar_events")).one()
    assert row.recurrence is None
    assert row.timezone is None
