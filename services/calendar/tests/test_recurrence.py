"""Tests for recurring events (#432): the db-level storage model + local provider expansion.

Covers the three row kinds (plain / series master / exception), instance-id round-tripping,
occurrence expansion within a window, ``edit_scope="this"`` vs ``"all"``, free/busy
accounting for recurring occurrences, and the two local-provider expansion edges fixed by
#446: DST wall-clock anchoring and windowing a moved occurrence by its actual (not original)
time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

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
    assert [m.event.id for m in masters] == [master.id]
    assert masters[0].timezone is None  # no timezone was passed to create_event


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


# ── DST wall-clock anchoring (#446) ──────────────────────────────────────────────


async def test_recurring_series_keeps_wall_clock_time_across_dst_fallback(
    provider: LocalCalendarProvider,
) -> None:
    """A timed series anchored to an IANA zone keeps its wall-clock hour across a DST
    change, instead of drifting by the UTC-offset delta once the zone falls back (#446)."""
    ny = ZoneInfo("America/New_York")
    start = datetime(2026, 10, 26, 13, 0, tzinfo=UTC)  # Mon Oct 26, 09:00 EDT (UTC-4)
    await provider.create_event(
        tenant_id="t1",
        title="Standup",
        start=start,
        end=start + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=4",
        recurrence_timezone="America/New_York",
    )
    events = await provider.list_events(
        tenant_id="t1",
        time_range=DateTimeRange(
            start=datetime(2026, 10, 1, tzinfo=UTC), end=datetime(2026, 12, 1, tzinfo=UTC)
        ),
    )
    assert len(events) == 4
    # Every occurrence reads 09:00 America/New_York wall time...
    assert all(e.start.astimezone(ny).strftime("%H:%M") == "09:00" for e in events)
    # ...even though the UTC instant shifts an hour once EST (UTC-5) takes over after the
    # Nov 1 2026 fall-back — Oct 26 is still EDT; Nov 2/9/16 are EST.
    assert [e.start.astimezone(UTC).hour for e in events] == [13, 14, 14, 14]


async def test_recurring_series_without_a_stored_timezone_expands_in_utc(
    provider: LocalCalendarProvider,
) -> None:
    """A legacy series with no stored anchor (created before #446, or via a direct store
    call that never passed one) keeps the pre-fix UTC-anchored expansion rather than
    erroring — back-compat for rows written before the ``timezone`` column existed."""
    start = datetime(2026, 10, 26, 13, 0, tzinfo=UTC)
    await provider.create_event(
        tenant_id="t1",
        title="Legacy",
        start=start,
        end=start + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=4",
        # recurrence_timezone omitted, as a pre-#446 row would be.
    )
    events = await provider.list_events(
        tenant_id="t1",
        time_range=DateTimeRange(
            start=datetime(2026, 10, 1, tzinfo=UTC), end=datetime(2026, 12, 1, tzinfo=UTC)
        ),
    )
    # No DST correction applied — every occurrence sticks to the fixed 13:00 UTC anchor.
    assert [e.start.astimezone(UTC).hour for e in events] == [13, 13, 13, 13]


async def test_all_day_series_ignores_recurrence_timezone(provider: LocalCalendarProvider) -> None:
    """An all-day series stays a floating date regardless of any stored timezone (ADR-0037)
    — #446's zone anchoring applies only to timed series."""
    start = datetime(2026, 10, 26, tzinfo=UTC)
    await provider.create_event(
        tenant_id="t1",
        title="Holiday week",
        start=start,
        end=start + timedelta(days=1),
        all_day=True,
        recurrence="FREQ=WEEKLY;COUNT=2",
        recurrence_timezone="America/New_York",
    )
    events = await provider.list_events(
        tenant_id="t1",
        time_range=DateTimeRange(
            start=datetime(2026, 10, 1, tzinfo=UTC), end=datetime(2026, 11, 15, tzinfo=UTC)
        ),
    )
    assert [e.start for e in events] == [
        datetime(2026, 10, 26, tzinfo=UTC),
        datetime(2026, 11, 2, tzinfo=UTC),
    ]


# ── FREQ=MONTHLY from a 31st-of-the-month anchor (#446 test nit) ─────────────────


async def test_monthly_recurrence_from_the_31st_skips_shorter_months(
    provider: LocalCalendarProvider,
) -> None:
    """Pins ``dateutil``'s default ``FREQ=MONTHLY`` behaviour anchored on the 31st: a month
    with no 31st day is skipped entirely (matching Google Calendar), not rolled to the
    nearest valid day."""
    start = datetime(2026, 1, 31, 9, 0, tzinfo=UTC)
    await provider.create_event(
        tenant_id="t1",
        title="Month-end review",
        start=start,
        end=start + timedelta(hours=1),
        recurrence="FREQ=MONTHLY;COUNT=6",
    )
    events = await provider.list_events(
        tenant_id="t1",
        time_range=DateTimeRange(
            start=datetime(2026, 1, 1, tzinfo=UTC), end=datetime(2026, 11, 1, tzinfo=UTC)
        ),
    )
    # Feb, Apr, Jun, Sep have no 31st — dateutil skips them rather than clamping.
    assert [(e.start.month, e.start.day) for e in events] == [
        (1, 31),
        (3, 31),
        (5, 31),
        (7, 31),
        (8, 31),
        (10, 31),
    ]


# ── Moved occurrences windowed by their actual time, not their original slot (#446) ──


async def test_moved_occurrence_appears_in_the_window_it_moved_into(
    provider: LocalCalendarProvider,
) -> None:
    """An occurrence rescheduled from an out-of-window slot to an in-window time must be
    found when listing that window — not just its original one (#446)."""
    master = await provider.create_event(
        tenant_id="t1",
        title="Standup",
        start=_dt(6),
        end=_dt(6) + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=4",
    )
    # July 27's occurrence moves three weeks earlier, into the July 1-15 window below.
    iid = instance_id(master.id, _dt(27))
    await provider.update_event(
        tenant_id="t1",
        event_id=iid,
        start=_dt(10),
        end=_dt(10) + timedelta(minutes=30),
        edit_scope="this",
    )
    events = await provider.list_events(
        tenant_id="t1", time_range=DateTimeRange(start=_dt(1), end=_dt(15))
    )
    # The regular July 6 & 13 occurrences, plus the moved-in one (originally July 27).
    assert sorted(e.start.day for e in events) == [6, 10, 13]
    moved = next(e for e in events if e.start.day == 10)
    assert moved.original_start == _dt(27)


async def test_moved_occurrence_out_of_window_does_not_leak(
    provider: LocalCalendarProvider,
) -> None:
    """An occurrence rescheduled out of the window it originally sat in must not appear
    there with its stale, out-of-window start (#446) — and must appear in its new window
    instead."""
    master = await provider.create_event(
        tenant_id="t1",
        title="Standup",
        start=_dt(6),
        end=_dt(6) + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=4",
    )
    # July 13's occurrence moves to July 22 — out of the July 1-15 window it originally sat in.
    iid = instance_id(master.id, _dt(13))
    await provider.update_event(
        tenant_id="t1",
        event_id=iid,
        start=_dt(22),
        end=_dt(22) + timedelta(minutes=30),
        edit_scope="this",
    )
    original_window = await provider.list_events(
        tenant_id="t1", time_range=DateTimeRange(start=_dt(1), end=_dt(15))
    )
    assert sorted(e.start.day for e in original_window) == [6]  # 13 moved away — must not leak
    new_window = await provider.list_events(
        tenant_id="t1", time_range=DateTimeRange(start=_dt(15), end=_dt(31))
    )
    assert sorted(e.start.day for e in new_window) == [20, 22, 27]
    moved = next(e for e in new_window if e.start.day == 22)
    assert moved.original_start == _dt(13)


async def test_excluded_occurrence_outside_the_window_does_not_leak_in(
    provider: LocalCalendarProvider,
) -> None:
    """A deleted (excluded) occurrence whose original slot falls outside the queried window
    must still never appear — the moved-into-window scan (#446) must not resurrect a
    tombstone just because the main loop never visits its original, out-of-window slot."""
    master = await provider.create_event(
        tenant_id="t1",
        title="Standup",
        start=_dt(6),
        end=_dt(6) + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=4",
    )
    # July 27 is outside the [1, 15) window queried below.
    iid = instance_id(master.id, _dt(27))
    assert await provider.delete_event(tenant_id="t1", event_id=iid, edit_scope="this") is True
    events = await provider.list_events(
        tenant_id="t1", time_range=DateTimeRange(start=_dt(1), end=_dt(15))
    )
    assert sorted(e.start.day for e in events) == [6, 13]


# ── edit_scope="following": splitting a series in two (#445) ────────────────────


async def _titles_by_day(
    provider: LocalCalendarProvider, *, start: int = 1, end: int = 31
) -> dict[int, tuple[str, str | None]]:
    """``{day: (title, recurring_event_id)}`` for every event in July [start, end)."""
    events = await provider.list_events(
        tenant_id="t1", time_range=DateTimeRange(start=_dt(start), end=_dt(end))
    )
    return {e.start.day: (e.title, e.recurring_event_id) for e in events}


async def test_update_following_splits_the_series_at_the_named_occurrence(
    provider: LocalCalendarProvider,
) -> None:
    master = await provider.create_event(
        tenant_id="t1",
        title="Standup",
        start=_dt(6),
        end=_dt(6) + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=4",  # 6, 13, 20, 27
    )
    iid = instance_id(master.id, _dt(20))
    new_series = await provider.update_event(
        tenant_id="t1", event_id=iid, title="Standup (async)", edit_scope="following"
    )
    assert new_series is not None
    assert new_series.id != master.id  # a genuinely new series, not the original master

    by_day = await _titles_by_day(provider)
    # Occurrences before the split keep the original title and series id.
    assert by_day[6] == ("Standup", master.id)
    assert by_day[13] == ("Standup", master.id)
    # The split occurrence and everything after it move to the new series.
    assert by_day[20] == ("Standup (async)", new_series.id)
    assert by_day[27] == ("Standup (async)", new_series.id)

    # The original master itself is truncated in place — no COUNT/UNTIL beyond July 13.
    original = await provider.get_event(tenant_id="t1", event_id=master.id)
    assert original is not None
    assert original.recurrence is not None
    assert "COUNT" not in original.recurrence
    assert "UNTIL=20260713T090000Z" in original.recurrence


async def test_update_following_can_move_the_split_occurrences_time(
    provider: LocalCalendarProvider,
) -> None:
    master = await provider.create_event(
        tenant_id="t1",
        title="Standup",
        start=_dt(6),
        end=_dt(6) + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=4",
    )
    iid = instance_id(master.id, _dt(20))
    await provider.update_event(
        tenant_id="t1",
        event_id=iid,
        start=_dt(20, 10),
        end=_dt(20, 10) + timedelta(minutes=30),
        edit_scope="following",
    )
    events = await provider.list_events(
        tenant_id="t1", time_range=DateTimeRange(start=_dt(1), end=_dt(31))
    )
    by_day = {e.start.day: e.start.hour for e in events}
    assert by_day == {6: 9, 13: 9, 20: 10, 27: 10}  # the tail continues at the new hour


async def test_update_following_preserves_a_later_occurrences_own_edit(
    provider: LocalCalendarProvider,
) -> None:
    """An occurrence already individually edited (edit_scope="this") keeps its own
    fields through a later "following" split — the bulk edit only sets the baseline for
    the split point and genuinely *unmodified* occurrences after it (#445)."""
    master = await provider.create_event(
        tenant_id="t1",
        title="Standup",
        start=_dt(6),
        end=_dt(6) + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=5",  # 6, 13, 20, 27, Aug 3
    )
    aug3 = _dt(6) + timedelta(weeks=4)
    await provider.update_event(
        tenant_id="t1",
        event_id=instance_id(master.id, aug3),
        title="Special",
        edit_scope="this",
    )
    new_series = await provider.update_event(
        tenant_id="t1",
        event_id=instance_id(master.id, _dt(20)),
        title="Standup (async)",
        edit_scope="following",
    )
    assert new_series is not None
    events = await provider.list_events(
        tenant_id="t1", time_range=DateTimeRange(start=_dt(1), end=aug3 + timedelta(days=1))
    )
    titles = {e.start.day: e.title for e in events}
    assert titles[20] == "Standup (async)"
    assert titles[27] == "Standup (async)"
    assert titles[aug3.day] == "Special"  # untouched by the bulk rename
    # ...but it still belongs to the new series now (repointed, #445).
    moved = next(e for e in events if e.start.day == aug3.day)
    assert moved.recurring_event_id == new_series.id


async def test_delete_following_removes_the_split_occurrence_and_every_later_one(
    provider: LocalCalendarProvider,
) -> None:
    master = await provider.create_event(
        tenant_id="t1",
        title="Standup",
        start=_dt(6),
        end=_dt(6) + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=4",
    )
    iid = instance_id(master.id, _dt(20))
    assert await provider.delete_event(tenant_id="t1", event_id=iid, edit_scope="following") is True
    events = await provider.list_events(
        tenant_id="t1", time_range=DateTimeRange(start=_dt(1), end=_dt(31))
    )
    assert sorted(e.start.day for e in events) == [6, 13]


async def test_delete_following_also_drops_a_later_occurrences_own_exception(
    provider: LocalCalendarProvider, store: LocalEventStore
) -> None:
    master = await provider.create_event(
        tenant_id="t1",
        title="Standup",
        start=_dt(6),
        end=_dt(6) + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=5",
    )
    aug3 = _dt(6) + timedelta(weeks=4)
    await provider.update_event(
        tenant_id="t1", event_id=instance_id(master.id, aug3), title="Special", edit_scope="this"
    )
    await provider.delete_event(
        tenant_id="t1", event_id=instance_id(master.id, _dt(20)), edit_scope="following"
    )
    # The Aug-3 exception is gone entirely, not merely orphaned.
    assert await store.list_exceptions(tenant="t1", series_id=master.id) == []


async def test_update_following_at_the_first_occurrence_edits_the_whole_series(
    provider: LocalCalendarProvider,
) -> None:
    master = await provider.create_event(
        tenant_id="t1",
        title="Standup",
        start=_dt(6),
        end=_dt(6) + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=4",
    )
    iid = instance_id(master.id, _dt(6))  # the series' very first occurrence
    updated = await provider.update_event(
        tenant_id="t1", event_id=iid, title="Renamed", edit_scope="following"
    )
    assert updated is not None
    assert updated.id == master.id  # no split — the same series, edited in place
    events = await provider.list_events(
        tenant_id="t1", time_range=DateTimeRange(start=_dt(1), end=_dt(31))
    )
    assert all(e.title == "Renamed" for e in events)
    assert len(events) == 4


async def test_delete_following_at_the_first_occurrence_deletes_the_whole_series(
    provider: LocalCalendarProvider, store: LocalEventStore
) -> None:
    master = await provider.create_event(
        tenant_id="t1",
        title="Standup",
        start=_dt(6),
        end=_dt(6) + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=4",
    )
    iid = instance_id(master.id, _dt(6))
    assert await provider.delete_event(tenant_id="t1", event_id=iid, edit_scope="following") is True
    assert await store.count(tenant="t1") == 0


async def test_update_following_can_override_the_continuation_recurrence(
    provider: LocalCalendarProvider,
) -> None:
    """ "Following" can also change the cadence itself for the tail, not just fields."""
    master = await provider.create_event(
        tenant_id="t1",
        title="Standup",
        start=_dt(6),
        end=_dt(6) + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=4",
    )
    iid = instance_id(master.id, _dt(20))
    new_series = await provider.update_event(
        tenant_id="t1", event_id=iid, recurrence="FREQ=DAILY;COUNT=3", edit_scope="following"
    )
    assert new_series is not None
    assert new_series.recurrence == "FREQ=DAILY;COUNT=3"
    events = await provider.list_events(
        tenant_id="t1", time_range=DateTimeRange(start=_dt(1), end=_dt(31))
    )
    assert sorted(e.start.day for e in events) == [6, 13, 20, 21, 22]  # old pair + new daily run


async def test_update_following_on_an_unbounded_series(provider: LocalCalendarProvider) -> None:
    master = await provider.create_event(
        tenant_id="t1",
        title="Daily",
        start=_dt(1),
        end=_dt(1) + timedelta(minutes=15),
        recurrence="FREQ=DAILY",  # no COUNT/UNTIL
    )
    iid = instance_id(master.id, _dt(10))
    await provider.update_event(
        tenant_id="t1", event_id=iid, title="Daily (renamed)", edit_scope="following"
    )
    original = await provider.get_event(tenant_id="t1", event_id=master.id)
    assert original is not None
    assert original.recurrence == "FREQ=DAILY;UNTIL=20260709T090000Z"
    events = await provider.list_events(
        tenant_id="t1", time_range=DateTimeRange(start=_dt(8), end=_dt(13))
    )
    titles = {e.start.day: e.title for e in events}
    assert titles == {
        8: "Daily",
        9: "Daily",
        10: "Daily (renamed)",
        11: "Daily (renamed)",
        12: "Daily (renamed)",
    }


async def test_update_following_carries_over_the_original_series_timezone(
    provider: LocalCalendarProvider,
) -> None:
    """A DST-anchored series (#446) keeps its stored timezone on the new tail series when
    the split doesn't supply a new one — the two fixes compose correctly (#445)."""
    ny = ZoneInfo("America/New_York")
    start = datetime(2026, 10, 26, 13, 0, tzinfo=UTC)  # Mon Oct 26, 09:00 EDT
    master = await provider.create_event(
        tenant_id="t1",
        title="Standup",
        start=start,
        end=start + timedelta(minutes=30),
        recurrence="FREQ=WEEKLY;COUNT=4",  # Oct 26, Nov 2, 9, 16
        recurrence_timezone="America/New_York",
    )
    iid = instance_id(master.id, datetime(2026, 11, 2, 14, 0, tzinfo=UTC))  # 09:00 EST
    await provider.update_event(
        tenant_id="t1", event_id=iid, title="Standup (split)", edit_scope="following"
    )
    events = await provider.list_events(
        tenant_id="t1",
        time_range=DateTimeRange(
            start=datetime(2026, 10, 1, tzinfo=UTC), end=datetime(2026, 12, 1, tzinfo=UTC)
        ),
    )
    assert len(events) == 4
    # Every occurrence — both series — still reads 09:00 America/New_York wall time.
    assert all(e.start.astimezone(ny).strftime("%H:%M") == "09:00" for e in events)
