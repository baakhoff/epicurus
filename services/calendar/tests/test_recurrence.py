"""Tests for recurring events (#432): the db-level storage model + local provider expansion.

Covers the three row kinds (plain / series master / exception), instance-id round-tripping,
occurrence expansion within a window, ``edit_scope="this"`` vs ``"all"``, and free/busy
accounting for recurring occurrences.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_calendar.db import LocalEventStore, instance_id, parse_instance_id
from epicurus_calendar.models import Attendee, DateTimeRange
from epicurus_calendar.providers.local import LocalCalendarProvider


def _dt(day: int, hour: int = 9) -> datetime:
    return datetime(2026, 7, day, hour, 0, tzinfo=UTC)


@pytest.fixture()
async def store() -> LocalEventStore:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    s = LocalEventStore(engine)
    await s.init()
    return s


@pytest.fixture()
def provider(store: LocalEventStore) -> LocalCalendarProvider:
    return LocalCalendarProvider(store=store)


# ── instance id round-trip ──────────────────────────────────────────────────────


def test_instance_id_round_trips() -> None:
    start = datetime(2026, 7, 10, 15, 0, tzinfo=UTC)
    iid = instance_id("series-1", start)
    assert iid == "series-1_20260710T150000Z"
    assert parse_instance_id(iid) == ("series-1", start)


def test_parse_instance_id_rejects_a_plain_uuid() -> None:
    # A bare event_id (uuid4, hyphens only) never contains "_", so it never
    # false-positives as an instance id.
    assert parse_instance_id("3fa85f64-5717-4562-b3fc-2c963f66afa6") is None


def test_parse_instance_id_rejects_a_malformed_suffix() -> None:
    assert parse_instance_id("series-1_not-a-timestamp") is None


# ── creating a series + basic expansion ─────────────────────────────────────────


async def test_create_recurring_event_stores_the_rrule(provider: LocalCalendarProvider) -> None:
    created = await provider.create_event(
        tenant_id="t1",
        title="Standup",
        start=_dt(6),
        end=_dt(6, 9) + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=4",
    )
    assert created.recurrence == "FREQ=WEEKLY;COUNT=4"
    assert created.recurring_event_id is None  # the master itself, not an instance


async def test_list_events_expands_a_weekly_series(provider: LocalCalendarProvider) -> None:
    await provider.create_event(
        tenant_id="t1",
        title="Standup",
        start=_dt(6),  # a Monday
        end=_dt(6) + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=4",
    )
    events = await provider.list_events(
        tenant_id="t1",
        time_range=DateTimeRange(start=_dt(1), end=_dt(31)),
    )
    assert len(events) == 4
    assert [e.start.day for e in events] == [6, 13, 20, 27]
    assert all(e.title == "Standup" for e in events)
    assert all(e.recurring_event_id is not None for e in events)


async def test_list_events_clips_expansion_to_the_window(provider: LocalCalendarProvider) -> None:
    await provider.create_event(
        tenant_id="t1",
        title="Standup",
        start=_dt(6),
        end=_dt(6) + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=10",
    )
    events = await provider.list_events(
        tenant_id="t1", time_range=DateTimeRange(start=_dt(1), end=_dt(15))
    )
    assert [e.start.day for e in events] == [6, 13]


async def test_list_events_bounds_an_unbounded_daily_rule(provider: LocalCalendarProvider) -> None:
    # No COUNT/UNTIL — must not hang or blow up; .between() bounds it to the window.
    await provider.create_event(
        tenant_id="t1",
        title="Daily",
        start=_dt(1),
        end=_dt(1) + timedelta(minutes=15),
        recurrence="FREQ=DAILY",
    )
    events = await provider.list_events(
        tenant_id="t1", time_range=DateTimeRange(start=_dt(1), end=_dt(8))
    )
    assert len(events) == 7


async def test_plain_and_recurring_events_coexist(provider: LocalCalendarProvider) -> None:
    await provider.create_event(tenant_id="t1", title="One-off", start=_dt(10), end=_dt(10, 10))
    await provider.create_event(
        tenant_id="t1",
        title="Weekly",
        start=_dt(6),
        end=_dt(6, 10),
        recurrence="FREQ=WEEKLY;COUNT=3",
    )
    events = await provider.list_events(
        tenant_id="t1", time_range=DateTimeRange(start=_dt(1), end=_dt(31))
    )
    titles = sorted(e.title for e in events)
    assert titles == ["One-off", "Weekly", "Weekly", "Weekly"]


async def test_a_corrupt_stored_rrule_is_skipped_not_fatal(
    store: LocalEventStore, provider: LocalCalendarProvider
) -> None:
    # Bypass service.py's write-time validation to simulate a legacy/corrupt row.
    await store.create_event(
        tenant=("t1"), title="Broken", start=_dt(6), end=_dt(6, 10), recurrence="not an rrule"
    )
    await store.create_event(tenant="t1", title="Fine", start=_dt(10), end=_dt(10, 10))
    events = await provider.list_events(
        tenant_id="t1", time_range=DateTimeRange(start=_dt(1), end=_dt(31))
    )
    assert [e.title for e in events] == ["Fine"]


# ── get_event on instances ──────────────────────────────────────────────────────


async def test_get_event_synthesizes_an_unmodified_instance(
    provider: LocalCalendarProvider,
) -> None:
    master = await provider.create_event(
        tenant_id="t1",
        title="Standup",
        start=_dt(6),
        end=_dt(6) + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=4",
    )
    iid = instance_id(master.id, _dt(13))
    found = await provider.get_event(tenant_id="t1", event_id=iid)
    assert found is not None
    assert found.start == _dt(13)
    assert found.recurring_event_id == master.id
    assert found.original_start == _dt(13)


async def test_get_event_rejects_a_forged_instance_slot(provider: LocalCalendarProvider) -> None:
    master = await provider.create_event(
        tenant_id="t1",
        title="Standup",
        start=_dt(6),
        end=_dt(6) + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=4",
    )
    # July 7 is a Tuesday — the series runs Mondays, so this is not a real occurrence.
    bad_id = instance_id(master.id, _dt(7))
    assert await provider.get_event(tenant_id="t1", event_id=bad_id) is None


async def test_get_event_on_a_non_recurring_series_id_component_returns_none(
    provider: LocalCalendarProvider,
) -> None:
    plain = await provider.create_event(
        tenant_id="t1", title="One-off", start=_dt(10), end=_dt(10, 10)
    )
    fake_instance = instance_id(plain.id, _dt(10))
    assert await provider.get_event(tenant_id="t1", event_id=fake_instance) is None


# ── edit_scope="this": per-occurrence exceptions ────────────────────────────────


async def test_update_this_occurrence_creates_an_override(provider: LocalCalendarProvider) -> None:
    master = await provider.create_event(
        tenant_id="t1",
        title="Standup",
        start=_dt(6),
        end=_dt(6) + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=4",
    )
    iid = instance_id(master.id, _dt(13))
    updated = await provider.update_event(
        tenant_id="t1", event_id=iid, title="Standup (moved)", edit_scope="this"
    )
    assert updated is not None
    assert updated.title == "Standup (moved)"
    assert updated.id == iid  # the instance id is stable across the edit

    events = await provider.list_events(
        tenant_id="t1", time_range=DateTimeRange(start=_dt(1), end=_dt(31))
    )
    titles = {e.start.day: e.title for e in events}
    assert titles == {6: "Standup", 13: "Standup (moved)", 20: "Standup", 27: "Standup"}


async def test_update_this_occurrence_can_move_its_time(provider: LocalCalendarProvider) -> None:
    master = await provider.create_event(
        tenant_id="t1",
        title="Standup",
        start=_dt(6),
        end=_dt(6) + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=4",
    )
    iid = instance_id(master.id, _dt(13))
    moved = await provider.update_event(
        tenant_id="t1",
        event_id=iid,
        start=_dt(13, 15),
        end=_dt(13, 15) + timedelta(minutes=30),
        edit_scope="this",
    )
    assert moved is not None
    assert moved.start == _dt(13, 15)


async def test_update_this_occurrence_partial_edit_preserves_the_rest(
    provider: LocalCalendarProvider,
) -> None:
    master = await provider.create_event(
        tenant_id="t1",
        title="Standup",
        start=_dt(6),
        end=_dt(6) + timedelta(minutes=30),
        location="Room 1",
        recurrence="FREQ=WEEKLY;COUNT=4",
    )
    iid = instance_id(master.id, _dt(13))
    updated = await provider.update_event(
        tenant_id="t1", event_id=iid, title="New", edit_scope="this"
    )
    assert updated is not None
    assert updated.location == "Room 1"  # untouched field carries over from the master


async def test_update_this_occurrence_twice_updates_the_same_exception(
    provider: LocalCalendarProvider,
) -> None:
    master = await provider.create_event(
        tenant_id="t1",
        title="Standup",
        start=_dt(6),
        end=_dt(6) + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=4",
    )
    iid = instance_id(master.id, _dt(13))
    await provider.update_event(tenant_id="t1", event_id=iid, title="First edit", edit_scope="this")
    await provider.update_event(
        tenant_id="t1", event_id=iid, title="Second edit", edit_scope="this"
    )
    events = await provider.list_events(
        tenant_id="t1", time_range=DateTimeRange(start=_dt(1), end=_dt(31))
    )
    assert sum(1 for e in events if e.start.day == 13) == 1  # no duplicate rows
    assert next(e for e in events if e.start.day == 13).title == "Second edit"


async def test_update_this_with_recurrence_raises(provider: LocalCalendarProvider) -> None:
    master = await provider.create_event(
        tenant_id="t1",
        title="Standup",
        start=_dt(6),
        end=_dt(6) + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=4",
    )
    iid = instance_id(master.id, _dt(13))
    with pytest.raises(ValueError, match="single occurrence"):
        await provider.update_event(
            tenant_id="t1", event_id=iid, recurrence="FREQ=DAILY", edit_scope="this"
        )


async def test_update_this_on_a_non_occurrence_returns_none(
    provider: LocalCalendarProvider,
) -> None:
    master = await provider.create_event(
        tenant_id="t1",
        title="Standup",
        start=_dt(6),
        end=_dt(6) + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=4",
    )
    bad_id = instance_id(master.id, _dt(7))  # a Tuesday — not a real Monday occurrence
    assert await provider.update_event(tenant_id="t1", event_id=bad_id, title="x") is None


# ── deleting a single occurrence (tombstone) ────────────────────────────────────


async def test_delete_this_occurrence_excludes_it_from_listing(
    provider: LocalCalendarProvider,
) -> None:
    master = await provider.create_event(
        tenant_id="t1",
        title="Standup",
        start=_dt(6),
        end=_dt(6) + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=4",
    )
    iid = instance_id(master.id, _dt(13))
    assert await provider.delete_event(tenant_id="t1", event_id=iid, edit_scope="this") is True
    events = await provider.list_events(
        tenant_id="t1", time_range=DateTimeRange(start=_dt(1), end=_dt(31))
    )
    assert [e.start.day for e in events] == [6, 20, 27]


async def test_delete_this_occurrence_is_idempotent_style_false_on_missing(
    provider: LocalCalendarProvider,
) -> None:
    master = await provider.create_event(
        tenant_id="t1",
        title="Standup",
        start=_dt(6),
        end=_dt(6) + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=4",
    )
    bad_id = instance_id(master.id, _dt(7))
    assert await provider.delete_event(tenant_id="t1", event_id=bad_id, edit_scope="this") is False


async def test_edited_then_deleted_occurrence_stays_excluded(
    provider: LocalCalendarProvider,
) -> None:
    master = await provider.create_event(
        tenant_id="t1",
        title="Standup",
        start=_dt(6),
        end=_dt(6) + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=4",
    )
    iid = instance_id(master.id, _dt(13))
    await provider.update_event(tenant_id="t1", event_id=iid, title="Moved", edit_scope="this")
    await provider.delete_event(tenant_id="t1", event_id=iid, edit_scope="this")
    events = await provider.list_events(
        tenant_id="t1", time_range=DateTimeRange(start=_dt(1), end=_dt(31))
    )
    assert [e.start.day for e in events] == [6, 20, 27]


# ── edit_scope="all": the whole series ──────────────────────────────────────────


async def test_update_all_changes_every_future_occurrence(provider: LocalCalendarProvider) -> None:
    master = await provider.create_event(
        tenant_id="t1",
        title="Standup",
        start=_dt(6),
        end=_dt(6) + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=4",
    )
    await provider.update_event(
        tenant_id="t1", event_id=master.id, title="Renamed", edit_scope="all"
    )
    events = await provider.list_events(
        tenant_id="t1", time_range=DateTimeRange(start=_dt(1), end=_dt(31))
    )
    assert all(e.title == "Renamed" for e in events)


async def test_update_all_given_an_instance_id_resolves_to_the_series(
    provider: LocalCalendarProvider,
) -> None:
    master = await provider.create_event(
        tenant_id="t1",
        title="Standup",
        start=_dt(6),
        end=_dt(6) + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=4",
    )
    iid = instance_id(master.id, _dt(13))
    updated = await provider.update_event(
        tenant_id="t1", event_id=iid, title="Renamed all", edit_scope="all"
    )
    assert updated is not None
    assert updated.id == master.id  # acted on the series, not the instance
    events = await provider.list_events(
        tenant_id="t1", time_range=DateTimeRange(start=_dt(1), end=_dt(31))
    )
    assert all(e.title == "Renamed all" for e in events)


async def test_delete_all_removes_the_series_and_its_exceptions(
    provider: LocalCalendarProvider,
) -> None:
    master = await provider.create_event(
        tenant_id="t1",
        title="Standup",
        start=_dt(6),
        end=_dt(6) + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=4",
    )
    iid = instance_id(master.id, _dt(13))
    await provider.update_event(tenant_id="t1", event_id=iid, title="Moved", edit_scope="this")
    assert await provider.delete_event(tenant_id="t1", event_id=master.id, edit_scope="all") is True
    events = await provider.list_events(
        tenant_id="t1", time_range=DateTimeRange(start=_dt(1), end=_dt(31))
    )
    assert events == []


async def test_delete_all_given_an_instance_id_resolves_to_the_series(
    provider: LocalCalendarProvider,
) -> None:
    master = await provider.create_event(
        tenant_id="t1",
        title="Standup",
        start=_dt(6),
        end=_dt(6) + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=4",
    )
    iid = instance_id(master.id, _dt(13))
    assert await provider.delete_event(tenant_id="t1", event_id=iid, edit_scope="all") is True
    events = await provider.list_events(
        tenant_id="t1", time_range=DateTimeRange(start=_dt(1), end=_dt(31))
    )
    assert events == []


# ── attendees round-trip ─────────────────────────────────────────────────────────


async def test_attendees_round_trip_through_create_and_read(
    provider: LocalCalendarProvider,
) -> None:
    guests = [Attendee(email="alice@example.com"), Attendee(email="bob@example.com")]
    created = await provider.create_event(
        tenant_id="t1", title="Sync", start=_dt(10), end=_dt(10, 10), attendees=guests
    )
    fetched = await provider.get_event(tenant_id="t1", event_id=created.id)
    assert fetched is not None
    assert [a.email for a in fetched.attendees] == ["alice@example.com", "bob@example.com"]
    assert all(a.response_status == "needsAction" for a in fetched.attendees)


async def test_attendees_on_a_recurring_instance_edit(provider: LocalCalendarProvider) -> None:
    master = await provider.create_event(
        tenant_id="t1",
        title="Standup",
        start=_dt(6),
        end=_dt(6) + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=4",
    )
    iid = instance_id(master.id, _dt(13))
    guests = [Attendee(email="carol@example.com")]
    updated = await provider.update_event(
        tenant_id="t1", event_id=iid, attendees=guests, edit_scope="this"
    )
    assert updated is not None
    assert [a.email for a in updated.attendees] == ["carol@example.com"]
    # The unmodified occurrences carry no attendees (the master had none).
    other = await provider.get_event(tenant_id="t1", event_id=instance_id(master.id, _dt(20)))
    assert other is not None
    assert other.attendees == []


# ── find_free_slots accounts for recurring occurrences ───────────────────────────


async def test_find_free_slots_treats_recurring_occurrences_as_busy(
    provider: LocalCalendarProvider,
) -> None:
    await provider.create_event(
        tenant_id="t1",
        title="Standup",
        start=_dt(6),
        end=_dt(6) + timedelta(hours=1),
        recurrence="FREQ=WEEKLY;COUNT=2",
    )
    slots = await provider.find_free_slots(
        tenant_id="t1",
        time_range=DateTimeRange(start=_dt(6), end=_dt(6) + timedelta(hours=2)),
        duration_minutes=60,
    )
    # The first hour is busy (the recurring occurrence); only the second hour is free.
    assert len(slots) == 1
    assert slots[0].start == _dt(6) + timedelta(hours=1)


# ── ExceptionRow / list_exceptions plumbing (db-level) ───────────────────────────


async def test_list_exceptions_reports_excluded_flag(store: LocalEventStore) -> None:
    master = await store.create_event(
        tenant="t1",
        title="Standup",
        start=_dt(6),
        end=_dt(6, 9) + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=4",
    )
    await store.upsert_exception(
        tenant="t1",
        series_id=master.id,
        original_start=_dt(13),
        title="Moved",
        start=_dt(13, 15),
        end=_dt(13, 15) + timedelta(minutes=30),
        description=None,
        location=None,
        all_day=False,
        excluded=False,
    )
    await store.upsert_exception(
        tenant="t1",
        series_id=master.id,
        original_start=_dt(20),
        title="",
        start=_dt(20),
        end=_dt(20),
        description=None,
        location=None,
        all_day=False,
        excluded=True,
    )
    exceptions = await store.list_exceptions(tenant="t1", series_id=master.id)
    by_start = {e.original_start: e.excluded for e in exceptions}
    assert by_start == {_dt(13): False, _dt(20): True}


async def test_list_master_events_excludes_non_recurring(store: LocalEventStore) -> None:
    await store.create_event(tenant="t1", title="One-off", start=_dt(10), end=_dt(10, 10))
    master = await store.create_event(
        tenant="t1",
        title="Standup",
        start=_dt(6),
        end=_dt(6, 9) + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=4",
    )
    masters = await store.list_master_events(tenant="t1", start=_dt(1), end=_dt(31))
    assert [m.id for m in masters] == [master.id]


async def test_delete_exceptions_for_removes_only_that_series(store: LocalEventStore) -> None:
    a = await store.create_event(
        tenant="t1",
        title="A",
        start=_dt(6),
        end=_dt(6, 9) + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=4",
    )
    b = await store.create_event(
        tenant="t1",
        title="B",
        start=_dt(6),
        end=_dt(6, 9) + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=4",
    )
    await store.upsert_exception(
        tenant="t1",
        series_id=a.id,
        original_start=_dt(13),
        title="A moved",
        start=_dt(13),
        end=_dt(13, 9) + timedelta(minutes=30),
        description=None,
        location=None,
        all_day=False,
        excluded=False,
    )
    await store.upsert_exception(
        tenant="t1",
        series_id=b.id,
        original_start=_dt(13),
        title="B moved",
        start=_dt(13),
        end=_dt(13, 9) + timedelta(minutes=30),
        description=None,
        location=None,
        all_day=False,
        excluded=False,
    )
    await store.delete_exceptions_for(tenant="t1", series_id=a.id)
    assert await store.list_exceptions(tenant="t1", series_id=a.id) == []
    assert len(await store.list_exceptions(tenant="t1", series_id=b.id)) == 1
