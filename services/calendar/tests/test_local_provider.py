"""Unit tests for the local calendar provider and free-slot algorithm.

Uses an in-memory SQLite database (aiosqlite) for speed — no Docker required.
The DB schema is identical to Postgres; only column types differ slightly, but
that does not affect the logic under test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
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
    await store.delete_event(tenant="t1", event_id=event.id)
    assert await store.count(tenant="t1") == 0


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
