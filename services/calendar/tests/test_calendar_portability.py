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

from epicurus_calendar.db import LocalEventStore, instance_id
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
