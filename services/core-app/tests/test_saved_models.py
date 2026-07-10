"""Unit tests for SavedHostedModelStore (in-memory SQLite, StaticPool) (#496)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

import epicurus_core_app.llm.saved_models as saved_models_mod
from epicurus_core_app.llm.saved_models import SavedHostedModelStore


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
