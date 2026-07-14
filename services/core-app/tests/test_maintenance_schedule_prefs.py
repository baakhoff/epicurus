"""Unit tests for the maintenance schedule (#621): the store, and the pure cadence functions.

The store follows the same in-memory-SQLite convention as ``test_timezone_prefs.py`` /
``test_page_order_prefs.py``. ``next_run_at``/``is_due`` are pure functions of constructed
datetimes — no real clock, no monkeypatched sleep — mirroring ``scheduled_turns``'s own
``_is_due`` test style.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core_app.maintenance_schedule_prefs import (
    MaintenanceSchedule,
    MaintenanceScheduleStore,
    is_due,
    next_run_at,
    validate_cadence,
)


async def _fresh(
    *, default_enabled: bool = False, default_hour: int = 4
) -> tuple[MaintenanceScheduleStore, AsyncEngine]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    store = MaintenanceScheduleStore(
        engine, default_enabled=default_enabled, default_hour=default_hour
    )
    await store.init()
    return store, engine


# ── store ────────────────────────────────────────────────────────────────────────


async def test_missing_row_falls_back_to_the_env_default() -> None:
    store, _ = await _fresh(default_enabled=True, default_hour=7)
    assert await store.get("t1") == MaintenanceSchedule(
        enabled=True, cadence="daily", hour=7, weekday=None
    )


async def test_set_and_get() -> None:
    store, _ = await _fresh()
    schedule = MaintenanceSchedule(enabled=True, cadence="weekly", hour=3, weekday=5)
    await store.set("t1", schedule)
    assert await store.get("t1") == schedule


async def test_set_is_tenant_scoped() -> None:
    store, _ = await _fresh(default_enabled=False, default_hour=4)
    await store.set("t1", MaintenanceSchedule(enabled=True, cadence="hourly", hour=0))
    assert await store.get("t2") == MaintenanceSchedule(
        enabled=False, cadence="daily", hour=4, weekday=None
    )


async def test_set_overwrites_a_previous_schedule_wholesale() -> None:
    store, _ = await _fresh()
    await store.set("t1", MaintenanceSchedule(enabled=True, cadence="weekly", hour=3, weekday=5))
    await store.set("t1", MaintenanceSchedule(enabled=False, cadence="daily", hour=9))
    # The weekly weekday from the first write must not leak into the second, unrelated schedule.
    assert await store.get("t1") == MaintenanceSchedule(
        enabled=False, cadence="daily", hour=9, weekday=None
    )


async def test_init_heals_legacy_table_missing_columns() -> None:
    """A pre-existing table with only the PK column self-heals rather than 500ing."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE maintenance_schedule_prefs (tenant VARCHAR(63) PRIMARY KEY)"
        )
    store = MaintenanceScheduleStore(engine, default_enabled=False, default_hour=4)
    await store.init()  # must ADD COLUMN rather than fail
    await store.set("t1", MaintenanceSchedule(enabled=True, cadence="daily", hour=6))
    assert await store.get("t1") == MaintenanceSchedule(
        enabled=True, cadence="daily", hour=6, weekday=None
    )


# ── validate_cadence ───────────────────────────────────────────────────────────────


def test_validate_cadence_accepts_every_valid_shape() -> None:
    validate_cadence("hourly", 0, None)
    validate_cadence("daily", 23, None)
    validate_cadence("weekly", 4, 6)


def test_validate_cadence_rejects_an_unknown_cadence() -> None:
    with pytest.raises(ValueError, match="cadence"):
        validate_cadence("monthly", 4, None)


def test_validate_cadence_rejects_an_out_of_range_hour() -> None:
    with pytest.raises(ValueError, match="hour"):
        validate_cadence("daily", 24, None)
    with pytest.raises(ValueError, match="hour"):
        validate_cadence("daily", -1, None)


def test_validate_cadence_requires_a_weekday_for_weekly() -> None:
    with pytest.raises(ValueError, match="weekday"):
        validate_cadence("weekly", 4, None)
    with pytest.raises(ValueError, match="weekday"):
        validate_cadence("weekly", 4, 7)


def test_validate_cadence_rejects_a_weekday_outside_weekly() -> None:
    with pytest.raises(ValueError, match="weekday"):
        validate_cadence("daily", 4, 0)
    with pytest.raises(ValueError, match="weekday"):
        validate_cadence("hourly", 0, 0)


# ── next_run_at ──────────────────────────────────────────────────────────────────


def test_next_run_at_hourly_is_the_top_of_the_next_hour() -> None:
    schedule = MaintenanceSchedule(enabled=True, cadence="hourly", hour=0)
    now = datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
    assert next_run_at(schedule, now) == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)


def test_next_run_at_daily_is_today_if_still_ahead() -> None:
    schedule = MaintenanceSchedule(enabled=True, cadence="daily", hour=16)
    now = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    assert next_run_at(schedule, now) == datetime(2026, 1, 1, 16, 0, tzinfo=UTC)


def test_next_run_at_daily_rolls_to_tomorrow_once_past() -> None:
    schedule = MaintenanceSchedule(enabled=True, cadence="daily", hour=4)
    now = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    assert next_run_at(schedule, now) == datetime(2026, 1, 2, 4, 0, tzinfo=UTC)


def test_next_run_at_weekly_targets_the_configured_weekday() -> None:
    # 2026-01-01 is a Thursday (weekday()==3); target Monday (0) at 04:00.
    schedule = MaintenanceSchedule(enabled=True, cadence="weekly", hour=4, weekday=0)
    now = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    assert next_run_at(schedule, now) == datetime(2026, 1, 5, 4, 0, tzinfo=UTC)


def test_next_run_at_weekly_rolls_to_next_week_once_todays_slot_has_passed() -> None:
    # 2026-01-05 is the target Monday, but 9:00 is already past the 4:00 target hour.
    schedule = MaintenanceSchedule(enabled=True, cadence="weekly", hour=4, weekday=0)
    now = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
    assert next_run_at(schedule, now) == datetime(2026, 1, 12, 4, 0, tzinfo=UTC)


def test_next_run_at_weekly_same_day_still_ahead() -> None:
    schedule = MaintenanceSchedule(enabled=True, cadence="weekly", hour=16, weekday=0)
    now = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
    assert next_run_at(schedule, now) == datetime(2026, 1, 5, 16, 0, tzinfo=UTC)


# ── is_due ───────────────────────────────────────────────────────────────────────


def test_is_due_false_when_disabled() -> None:
    schedule = MaintenanceSchedule(enabled=False, cadence="daily", hour=4)
    assert is_due(schedule, datetime(2026, 1, 1, 4, 0, tzinfo=UTC), None) is False


def test_is_due_daily_matches_only_the_configured_hour() -> None:
    schedule = MaintenanceSchedule(enabled=True, cadence="daily", hour=4)
    assert is_due(schedule, datetime(2026, 1, 1, 4, 30, tzinfo=UTC), None) is True
    assert is_due(schedule, datetime(2026, 1, 1, 9, 0, tzinfo=UTC), None) is False


def test_is_due_daily_does_not_refire_the_same_local_date() -> None:
    schedule = MaintenanceSchedule(enabled=True, cadence="daily", hour=4)
    last_fired = datetime(2026, 1, 1, 4, 5, tzinfo=UTC)
    assert is_due(schedule, datetime(2026, 1, 1, 4, 45, tzinfo=UTC), last_fired) is False
    assert is_due(schedule, datetime(2026, 1, 2, 4, 5, tzinfo=UTC), last_fired) is True


def test_is_due_weekly_requires_the_matching_weekday() -> None:
    schedule = MaintenanceSchedule(enabled=True, cadence="weekly", hour=4, weekday=0)
    assert is_due(schedule, datetime(2026, 1, 1, 4, 30, tzinfo=UTC), None) is False  # Thursday
    assert is_due(schedule, datetime(2026, 1, 5, 4, 30, tzinfo=UTC), None) is True  # Monday


def test_is_due_hourly_matches_any_hour_but_not_twice() -> None:
    schedule = MaintenanceSchedule(enabled=True, cadence="hourly", hour=0)
    last_fired = datetime(2026, 1, 1, 9, 5, tzinfo=UTC)
    assert is_due(schedule, datetime(2026, 1, 1, 9, 50, tzinfo=UTC), last_fired) is False
    assert is_due(schedule, datetime(2026, 1, 1, 10, 5, tzinfo=UTC), last_fired) is True
