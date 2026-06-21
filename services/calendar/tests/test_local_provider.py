"""Unit tests for the local calendar provider and free-slot algorithm.

Uses an in-memory SQLite database (aiosqlite) for speed — no Docker required.
The DB schema is identical to Postgres; only column types differ slightly, but
that does not affect the logic under test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_calendar.db import LocalEventStore
from epicurus_calendar.models import DateTimeRange
from epicurus_calendar.providers.local import LocalCalendarProvider, _compute_free_slots


def _dt(hour: int, minute: int = 0) -> datetime:
    return datetime(2025, 6, 15, hour, minute, 0, tzinfo=UTC)


@pytest.fixture()
async def store() -> LocalEventStore:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    s = LocalEventStore(engine)
    await s.init()
    return s


@pytest.fixture()
def provider(store: LocalEventStore) -> LocalCalendarProvider:
    return LocalCalendarProvider(store=store)


# ── LocalEventStore ──────────────────────────────────────────────────────────


async def test_create_and_list(store: LocalEventStore) -> None:
    await store.create_event(
        tenant="t1",
        title="Meeting",
        start=_dt(9),
        end=_dt(10),
    )
    events = await store.list_events(tenant="t1", start=_dt(8), end=_dt(11))
    assert len(events) == 1
    assert events[0].title == "Meeting"
    assert events[0].provider == "local"


async def test_list_filters_by_overlap(store: LocalEventStore) -> None:
    await store.create_event(tenant="t1", title="Early", start=_dt(6), end=_dt(7))
    await store.create_event(tenant="t1", title="Late", start=_dt(20), end=_dt(21))
    events = await store.list_events(tenant="t1", start=_dt(8), end=_dt(18))
    assert events == []


async def test_list_tenant_isolation(store: LocalEventStore) -> None:
    await store.create_event(tenant="t1", title="A", start=_dt(9), end=_dt(10))
    await store.create_event(tenant="t2", title="B", start=_dt(9), end=_dt(10))
    t1_events = await store.list_events(tenant="t1", start=_dt(8), end=_dt(11))
    assert len(t1_events) == 1
    assert t1_events[0].title == "A"


async def test_count(store: LocalEventStore) -> None:
    assert await store.count(tenant="t1") == 0
    await store.create_event(tenant="t1", title="X", start=_dt(9), end=_dt(10))
    assert await store.count(tenant="t1") == 1


async def test_delete_event(store: LocalEventStore) -> None:
    event = await store.create_event(tenant="t1", title="Gone", start=_dt(9), end=_dt(10))
    assert await store.delete_event(tenant="t1", event_id=event.id) is True
    assert await store.count(tenant="t1") == 0


async def test_delete_missing_event_returns_false(store: LocalEventStore) -> None:
    assert await store.delete_event(tenant="t1", event_id="nope") is False


async def test_update_event_applies_partial_fields(store: LocalEventStore) -> None:
    event = await store.create_event(
        tenant="t1", title="Old", start=_dt(9), end=_dt(10), description="d", location="here"
    )
    updated = await store.update_event(tenant="t1", event_id=event.id, title="New", start=_dt(11))
    assert updated is not None
    assert updated.title == "New"
    assert updated.start == _dt(11)
    assert updated.end == _dt(10)  # unchanged
    assert updated.description == "d"  # unchanged
    assert updated.location == "here"  # unchanged


async def test_update_missing_event_returns_none(store: LocalEventStore) -> None:
    assert await store.update_event(tenant="t1", event_id="nope", title="x") is None


async def test_update_event_is_tenant_scoped(store: LocalEventStore) -> None:
    created = await store.create_event(tenant="t1", title="Owned", start=_dt(9), end=_dt(10))
    # Another tenant must not edit t1's event.
    assert await store.update_event(tenant="t2", event_id=created.id, title="Hacked") is None
    still = await store.get_event(tenant="t1", event_id=created.id)
    assert still is not None and still.title == "Owned"


async def test_get_event_returns_match(store: LocalEventStore) -> None:
    created = await store.create_event(tenant="t1", title="Find me", start=_dt(9), end=_dt(10))
    fetched = await store.get_event(tenant="t1", event_id=created.id)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.title == "Find me"


async def test_get_event_missing_returns_none(store: LocalEventStore) -> None:
    assert await store.get_event(tenant="t1", event_id="missing") is None


async def test_get_event_is_tenant_scoped(store: LocalEventStore) -> None:
    created = await store.create_event(tenant="t1", title="Owned", start=_dt(9), end=_dt(10))
    # Another tenant must not resolve t1's event id.
    assert await store.get_event(tenant="t2", event_id=created.id) is None


async def test_all_day_event_round_trips(store: LocalEventStore) -> None:
    created = await store.create_event(
        tenant="t1",
        title="Holiday",
        start=datetime(2026, 6, 15, tzinfo=UTC),
        end=datetime(2026, 6, 16, tzinfo=UTC),
        all_day=True,
    )
    assert created.all_day is True
    fetched = await store.get_event(tenant="t1", event_id=created.id)
    assert fetched is not None and fetched.all_day is True


async def test_timed_event_defaults_all_day_false(store: LocalEventStore) -> None:
    created = await store.create_event(tenant="t1", title="Call", start=_dt(9), end=_dt(10))
    assert created.all_day is False


async def test_init_heals_table_missing_all_day_column() -> None:
    """A table provisioned before ``all_day`` existed is reconciled in place on init.

    Mirrors the tasks #248 drift fix: ``create_all`` never alters an existing table, so
    ``_ensure_columns`` must add the column or every local event read 500s. A legacy row
    reads back with ``all_day`` defaulted to ``False`` (NULL coerced).
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    # Build the pre-all_day schema and seed a row, bypassing the ORM model.
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE calendar_events ("
                "id INTEGER PRIMARY KEY, tenant VARCHAR(63), event_id VARCHAR(64),"
                "title VARCHAR(512), start_dt DATETIME, end_dt DATETIME,"
                "description TEXT, location VARCHAR(512), created_at DATETIME)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO calendar_events (tenant, event_id, title, start_dt, end_dt)"
                " VALUES ('t1', 'legacy-1', 'Old event', '2026-06-15 09:00:00',"
                " '2026-06-15 10:00:00')"
            )
        )

    store = LocalEventStore(engine)
    await store.init()  # adds the missing all_day column

    fetched = await store.get_event(tenant="t1", event_id="legacy-1")
    assert fetched is not None
    assert fetched.title == "Old event"
    assert fetched.all_day is False  # NULL legacy value coerced to False
    # init() is idempotent — a second call must not fail trying to re-add the column.
    await store.init()


# ── LocalCalendarProvider ────────────────────────────────────────────────────


async def test_provider_create_and_list(provider: LocalCalendarProvider) -> None:
    await provider.create_event(
        tenant_id="t1",
        title="Review",
        start=_dt(14),
        end=_dt(15),
        description="Weekly review",
    )
    events = await provider.list_events(
        tenant_id="t1",
        time_range=DateTimeRange(start=_dt(13), end=_dt(16)),
    )
    assert len(events) == 1
    assert events[0].description == "Weekly review"


async def test_provider_is_available(provider: LocalCalendarProvider) -> None:
    assert await provider.is_available(tenant_id="t1") is True


async def test_provider_get_event(provider: LocalCalendarProvider) -> None:
    created = await provider.create_event(
        tenant_id="t1", title="Review", start=_dt(14), end=_dt(15)
    )
    fetched = await provider.get_event(tenant_id="t1", event_id=created.id)
    assert fetched is not None
    assert fetched.title == "Review"
    assert await provider.get_event(tenant_id="t1", event_id="nope") is None


# ── Free-slot algorithm ──────────────────────────────────────────────────────


def test_no_events_whole_window_is_free() -> None:
    start = _dt(9)
    end = _dt(17)
    slots = _compute_free_slots(
        busy=[],
        window_start=start,
        window_end=end,
        min_duration=timedelta(hours=1),
    )
    assert len(slots) == 1
    assert slots[0].start == start
    assert slots[0].end == end


def test_busy_block_splits_window() -> None:
    slots = _compute_free_slots(
        busy=[(_dt(12), _dt(13))],
        window_start=_dt(9),
        window_end=_dt(17),
        min_duration=timedelta(hours=1),
    )
    assert len(slots) == 2
    assert slots[0].start == _dt(9) and slots[0].end == _dt(12)
    assert slots[1].start == _dt(13) and slots[1].end == _dt(17)


def test_overlapping_busy_intervals_are_merged() -> None:
    slots = _compute_free_slots(
        busy=[(_dt(10), _dt(12)), (_dt(11), _dt(13))],
        window_start=_dt(9),
        window_end=_dt(17),
        min_duration=timedelta(hours=1),
    )
    assert len(slots) == 2
    assert slots[0].end == _dt(10)
    assert slots[1].start == _dt(13)


def test_short_gaps_are_excluded() -> None:
    slots = _compute_free_slots(
        busy=[(_dt(9, 30), _dt(9, 45))],
        window_start=_dt(9),
        window_end=_dt(10),
        min_duration=timedelta(hours=1),
    )
    # The 30-min gap before and 15-min gap after are both too short.
    assert slots == []


def test_busy_fills_whole_window() -> None:
    slots = _compute_free_slots(
        busy=[(_dt(9), _dt(17))],
        window_start=_dt(9),
        window_end=_dt(17),
        min_duration=timedelta(hours=1),
    )
    assert slots == []


async def test_provider_find_free(provider: LocalCalendarProvider) -> None:
    await provider.create_event(tenant_id="t1", title="Blocked", start=_dt(12), end=_dt(13))
    slots = await provider.find_free_slots(
        tenant_id="t1",
        time_range=DateTimeRange(start=_dt(9), end=_dt(17)),
        duration_minutes=60,
    )
    assert any(s.end <= _dt(12) for s in slots)
    assert any(s.start >= _dt(13) for s in slots)
