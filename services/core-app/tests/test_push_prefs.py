"""Unit tests for PushPrefsStore, ChannelPrefs.effective(), and is_quiet_now()."""

from __future__ import annotations

from datetime import time

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core_app.push.prefs import (
    ChannelPrefs,
    PushPrefs,
    PushPrefsStore,
    is_quiet_now,
    validate_hhmm,
)

TENANT = "t1"


async def _store() -> PushPrefsStore:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    store = PushPrefsStore(engine)
    await store.init()
    return store


# ── PushPrefsStore ─────────────────────────────────────────────────────────────


async def test_get_on_an_unset_tenant_returns_defaults() -> None:
    store = await _store()
    prefs = await store.get(TENANT)
    assert prefs.categories == {}
    assert prefs.quiet_hours_enabled is False
    assert prefs.quiet_hours_start == "22:00"
    assert prefs.quiet_hours_end == "07:00"


async def test_set_categories_persists_and_merges() -> None:
    store = await _store()
    await store.set_categories(TENANT, {"mail": ChannelPrefs(push=False, center=True)})
    await store.set_categories(TENANT, {"tasks": ChannelPrefs(push=True, center=False)})
    prefs = await store.get(TENANT)
    assert prefs.categories["mail"] == ChannelPrefs(push=False, center=True)
    assert prefs.categories["tasks"] == ChannelPrefs(push=True, center=False)


async def test_set_categories_is_tenant_scoped() -> None:
    store = await _store()
    await store.set_categories(TENANT, {"mail": ChannelPrefs(push=False, center=False)})
    other = await store.get("other")
    assert other.categories == {}


async def test_set_quiet_hours_persists() -> None:
    store = await _store()
    await store.set_quiet_hours(TENANT, enabled=True, start="23:00", end="06:30")
    prefs = await store.get(TENANT)
    assert prefs.quiet_hours_enabled is True
    assert prefs.quiet_hours_start == "23:00"
    assert prefs.quiet_hours_end == "06:30"


async def test_set_quiet_hours_rejects_a_bad_time() -> None:
    store = await _store()
    with pytest.raises(ValueError, match="HH:MM"):
        await store.set_quiet_hours(TENANT, enabled=True, start="25:99", end="07:00")


async def test_automation_override_set_and_clear() -> None:
    store = await _store()
    await store.set_automation_override(TENANT, "auto-1", ChannelPrefs(push=False, center=True))
    prefs = await store.get(TENANT)
    assert prefs.automation_overrides["auto-1"] == ChannelPrefs(push=False, center=True)
    await store.set_automation_override(TENANT, "auto-1", None)
    prefs = await store.get(TENANT)
    assert "auto-1" not in prefs.automation_overrides


async def test_init_heals_a_legacy_table_missing_columns() -> None:
    """A pre-existing table missing every added column self-heals (mirrors timezone_prefs)."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE push_prefs (tenant VARCHAR(63) PRIMARY KEY)")
        await conn.exec_driver_sql("INSERT INTO push_prefs (tenant) VALUES ('t1')")
    store = PushPrefsStore(engine)
    await store.init()  # must ADD COLUMN rather than fail
    prefs = await store.get(TENANT)
    assert prefs.quiet_hours_enabled is False
    await store.set_quiet_hours(TENANT, enabled=True, start="22:00", end="07:00")
    assert (await store.get(TENANT)).quiet_hours_enabled is True


# ── PushPrefs.effective ──────────────────────────────────────────────────────────


def test_effective_defaults_to_on_on_for_an_unknown_category() -> None:
    prefs = PushPrefs()
    assert prefs.effective("mail") == ChannelPrefs(push=True, center=True)


def test_effective_uses_the_category_override() -> None:
    prefs = PushPrefs(categories={"mail": ChannelPrefs(push=False, center=True)})
    assert prefs.effective("mail") == ChannelPrefs(push=False, center=True)


def test_effective_prefers_the_automation_override_over_the_category() -> None:
    prefs = PushPrefs(
        categories={"automation": ChannelPrefs(push=True, center=True)},
        automation_overrides={"auto-1": ChannelPrefs(push=False, center=False)},
    )
    assert prefs.effective("automation", automation_id="auto-1") == ChannelPrefs(
        push=False, center=False
    )


def test_effective_falls_back_to_category_when_automation_has_no_override() -> None:
    prefs = PushPrefs(
        categories={"automation": ChannelPrefs(push=False, center=True)},
        automation_overrides={"other-automation": ChannelPrefs(push=True, center=True)},
    )
    assert prefs.effective("automation", automation_id="auto-1") == ChannelPrefs(
        push=False, center=True
    )


# ── validate_hhmm ────────────────────────────────────────────────────────────────


def test_validate_hhmm_accepts_valid_times() -> None:
    validate_hhmm("00:00")
    validate_hhmm("23:59")
    validate_hhmm("07:30")


@pytest.mark.parametrize("value", ["25:00", "not-a-time", "7:30", "", "12:60"])
def test_validate_hhmm_rejects_invalid_times(value: str) -> None:
    with pytest.raises(ValueError, match="HH:MM"):
        validate_hhmm(value)


# ── is_quiet_now ─────────────────────────────────────────────────────────────────


def test_disabled_quiet_hours_is_never_quiet() -> None:
    prefs = PushPrefs(quiet_hours_enabled=False, quiet_hours_start="22:00", quiet_hours_end="07:00")
    assert is_quiet_now(prefs, time(2, 0)) is False


def test_same_day_window_is_quiet_inside_and_not_outside() -> None:
    prefs = PushPrefs(quiet_hours_enabled=True, quiet_hours_start="13:00", quiet_hours_end="15:00")
    assert is_quiet_now(prefs, time(14, 0)) is True
    assert is_quiet_now(prefs, time(12, 59)) is False
    assert is_quiet_now(prefs, time(15, 0)) is False  # end is exclusive


def test_wraparound_window_is_quiet_across_midnight() -> None:
    prefs = PushPrefs(quiet_hours_enabled=True, quiet_hours_start="22:00", quiet_hours_end="07:00")
    assert is_quiet_now(prefs, time(23, 0)) is True  # before midnight
    assert is_quiet_now(prefs, time(3, 0)) is True  # after midnight
    assert is_quiet_now(prefs, time(12, 0)) is False  # midday, outside the window
    assert is_quiet_now(prefs, time(22, 0)) is True  # start is inclusive
    assert is_quiet_now(prefs, time(7, 0)) is False  # end is exclusive


def test_zero_width_window_is_never_quiet() -> None:
    prefs = PushPrefs(quiet_hours_enabled=True, quiet_hours_start="09:00", quiet_hours_end="09:00")
    assert is_quiet_now(prefs, time(9, 0)) is False
    assert is_quiet_now(prefs, time(12, 0)) is False
