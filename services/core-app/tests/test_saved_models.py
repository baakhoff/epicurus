"""Unit tests for SavedHostedModelStore (in-memory SQLite, StaticPool) (#496)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

import epicurus_core_app.llm.saved_models as saved_models_mod
from epicurus_core_app.llm.saved_models import SavedHostedModelStore, SavedModelOverride


async def _fresh() -> tuple[SavedHostedModelStore, AsyncEngine]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    store = SavedHostedModelStore(engine)
    await store.init()
    return store, engine


async def test_empty_by_default() -> None:
    store, _ = await _fresh()
    assert await store.list("t1") == []


async def test_add_and_list() -> None:
    store, _ = await _fresh()
    await store.add("t1", "claude/claude-3-5-sonnet-latest")
    assert await store.list("t1") == ["claude/claude-3-5-sonnet-latest"]


async def test_add_is_idempotent_no_duplicates() -> None:
    store, _ = await _fresh()
    await store.add("t1", "gpt/gpt-4o")
    await store.add("t1", "gpt/gpt-4o")
    assert await store.list("t1") == ["gpt/gpt-4o"]


async def test_concurrent_first_saves_upsert_to_one_row() -> None:
    """Several concurrent first-saves of the same new id upsert to a single row instead of racing
    between the get and the insert to a composite-PK IntegrityError (a 500) — the #537 fix. The
    upsert must not raise, and exactly one row survives."""
    store, _ = await _fresh()
    await asyncio.gather(*(store.add("t1", "claude/opus-4") for _ in range(4)))
    assert await store.list("t1") == ["claude/opus-4"]


async def test_list_is_most_recent_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ordering is by save time, newest first; a re-save bumps an id to the front."""
    store, _ = await _fresh()
    clock = iter([1_000, 2_000, 3_000, 4_000])
    monkeypatch.setattr(saved_models_mod, "_now_ms", lambda: next(clock))
    await store.add("t1", "claude/sonnet")  # t=1000
    await store.add("t1", "gpt/gpt-4o")  # t=2000
    assert await store.list("t1") == ["gpt/gpt-4o", "claude/sonnet"]
    await store.add("t1", "claude/sonnet")  # t=3000 — re-save bumps it to the front
    assert await store.list("t1") == ["claude/sonnet", "gpt/gpt-4o"]


async def test_add_is_tenant_scoped() -> None:
    store, _ = await _fresh()
    await store.add("t1", "claude/sonnet")
    assert await store.list("t2") == []


async def test_remove() -> None:
    store, _ = await _fresh()
    await store.add("t1", "gpt/gpt-4o")
    await store.remove("t1", "gpt/gpt-4o")
    assert await store.list("t1") == []


async def test_remove_absent_is_noop() -> None:
    store, _ = await _fresh()
    await store.remove("t1", "gpt/never-saved")  # must not raise
    assert await store.list("t1") == []


async def test_init_heals_legacy_table_without_added_at_column() -> None:
    """A pre-existing table missing ``added_at`` is migrated in place (mirrors llm_prefs)."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE saved_models (tenant VARCHAR(63), model VARCHAR(256), "
            "PRIMARY KEY (tenant, model))"
        )
        await conn.exec_driver_sql(
            "INSERT INTO saved_models (tenant, model) VALUES ('t1', 'claude/sonnet')"
        )
    store = SavedHostedModelStore(engine)
    await store.init()  # must ADD COLUMN added_at rather than fail
    assert await store.list("t1") == ["claude/sonnet"]
    await store.add("t1", "gpt/gpt-4o")
    assert set(await store.list("t1")) == {"claude/sonnet", "gpt/gpt-4o"}


# ── capability overrides (#711) ───────────────────────────────────────────────


async def test_no_override_by_default() -> None:
    store, _ = await _fresh()
    await store.add("t1", "xai/grok-latest")
    override = await store.get_override("t1", "xai/grok-latest")
    # Defaults are exactly the pre-override behaviour: ask the map, assume nothing.
    assert override.vision == "auto"
    assert override.context_length is None
    assert override.is_empty()
    assert await store.overrides("t1") == {}


async def test_set_and_read_back_an_override() -> None:
    store, _ = await _fresh()
    await store.add("t1", "xai/grok-latest")
    assert (
        await store.set_override(
            "t1", "xai/grok-latest", SavedModelOverride(vision="on", context_length=256_000)
        )
        is True
    )
    override = await store.get_override("t1", "xai/grok-latest")
    assert override.vision == "on"
    assert override.context_length == 256_000
    assert await store.overrides("t1") == {"xai/grok-latest": override}


async def test_clearing_an_override_restores_the_map_defaults() -> None:
    store, _ = await _fresh()
    await store.add("t1", "xai/grok-latest")
    await store.set_override("t1", "xai/grok-latest", SavedModelOverride(vision="off"))
    assert await store.set_override("t1", "xai/grok-latest", SavedModelOverride()) is True
    assert (await store.get_override("t1", "xai/grok-latest")).is_empty()
    # An empty override is absent from the map, not stored as an all-defaults row.
    assert await store.overrides("t1") == {}


async def test_set_override_for_an_unsaved_model_reports_failure() -> None:
    """An override belongs to a saved row — it must never conjure one (the route 404s on this)."""
    store, _ = await _fresh()
    assert (
        await store.set_override("t1", "gpt/never-saved", SavedModelOverride(vision="on")) is False
    )
    assert await store.list("t1") == []


async def test_overrides_are_tenant_scoped() -> None:
    store, _ = await _fresh()
    await store.add("t1", "xai/grok-latest")
    await store.add("t2", "xai/grok-latest")
    await store.set_override("t1", "xai/grok-latest", SavedModelOverride(vision="on"))
    assert (await store.get_override("t1", "xai/grok-latest")).vision == "on"
    assert (await store.get_override("t2", "xai/grok-latest")).vision == "auto"
    assert await store.overrides("t2") == {}


async def test_get_override_for_an_unsaved_model_is_defaults_not_an_error() -> None:
    store, _ = await _fresh()
    assert (await store.get_override("t1", "gpt/never-saved")).is_empty()


async def test_removing_a_model_takes_its_override_with_it() -> None:
    store, _ = await _fresh()
    await store.add("t1", "xai/grok-latest")
    await store.set_override("t1", "xai/grok-latest", SavedModelOverride(vision="on"))
    await store.remove("t1", "xai/grok-latest")
    await store.add("t1", "xai/grok-latest")  # re-saved: a fresh row, no stale override
    assert (await store.get_override("t1", "xai/grok-latest")).is_empty()


async def test_an_unknown_stored_vision_value_degrades_to_auto() -> None:
    """A value written by a newer build (or by hand) must not break the list it decorates."""
    store, engine = await _fresh()
    await store.add("t1", "xai/grok-latest")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "UPDATE saved_models SET vision_override = 'maybe' WHERE tenant = 't1'"
        )
    assert (await store.get_override("t1", "xai/grok-latest")).vision == "auto"


async def test_init_heals_a_table_without_the_override_columns() -> None:
    """A table provisioned before #711 gains both columns in place rather than 500ing."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE saved_models (tenant VARCHAR(63), model VARCHAR(256), "
            "added_at BIGINT, PRIMARY KEY (tenant, model))"
        )
        await conn.exec_driver_sql(
            "INSERT INTO saved_models (tenant, model, added_at) VALUES ('t1', 'xai/grok-latest', 1)"
        )
    store = SavedHostedModelStore(engine)
    await store.init()
    assert (await store.get_override("t1", "xai/grok-latest")).is_empty()
    assert (
        await store.set_override("t1", "xai/grok-latest", SavedModelOverride(vision="on")) is True
    )
    assert (await store.get_override("t1", "xai/grok-latest")).vision == "on"
