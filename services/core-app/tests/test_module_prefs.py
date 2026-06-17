"""Unit tests for ModulePrefsStore — tenant-scoped per-module enable flag (#126).

Uses an in-memory SQLite database with StaticPool (same pattern as test_llm_prefs.py).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core_app.module_prefs import ModulePrefsStore


async def _fresh_store() -> tuple[ModulePrefsStore, AsyncEngine]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    store = ModulePrefsStore(engine)
    await store.init()
    return store, engine


async def test_unset_module_is_enabled_by_default() -> None:
    store, _ = await _fresh_store()
    assert await store.is_enabled("t1", "tasks") is True


async def test_enabled_map_is_empty_until_set() -> None:
    store, _ = await _fresh_store()
    assert await store.enabled_map("t1") == {}


async def test_set_and_read_disabled() -> None:
    store, _ = await _fresh_store()
    await store.set_enabled("t1", "tasks", False)
    assert await store.is_enabled("t1", "tasks") is False
    assert await store.enabled_map("t1") == {"tasks": False}


async def test_re_enable_round_trips() -> None:
    store, _ = await _fresh_store()
    await store.set_enabled("t1", "tasks", False)
    await store.set_enabled("t1", "tasks", True)
    assert await store.is_enabled("t1", "tasks") is True


async def test_flag_is_tenant_scoped() -> None:
    store, _ = await _fresh_store()
    await store.set_enabled("t1", "tasks", False)
    assert await store.is_enabled("t2", "tasks") is True
    assert await store.enabled_map("t2") == {}


async def test_flag_is_module_scoped() -> None:
    store, _ = await _fresh_store()
    await store.set_enabled("t1", "tasks", False)
    assert await store.is_enabled("t1", "calendar") is True


async def test_repeated_updates_keep_one_row() -> None:
    store, _ = await _fresh_store()
    await store.set_enabled("t1", "tasks", False)
    await store.set_enabled("t1", "tasks", True)
    await store.set_enabled("t1", "tasks", False)
    # A single (tenant, module) row, last write wins.
    assert await store.enabled_map("t1") == {"tasks": False}


# ── Removal tombstone (#127) ───────────────────────────────────────────────────


async def test_no_modules_removed_by_default() -> None:
    store, _ = await _fresh_store()
    assert await store.removed_modules("t1") == set()


async def test_set_removed_tombstones_module() -> None:
    store, _ = await _fresh_store()
    await store.set_removed("t1", "tasks", True)
    assert await store.removed_modules("t1") == {"tasks"}


async def test_tombstone_is_tenant_scoped() -> None:
    store, _ = await _fresh_store()
    await store.set_removed("t1", "tasks", True)
    assert await store.removed_modules("t2") == set()


async def test_clear_tombstone() -> None:
    store, _ = await _fresh_store()
    await store.set_removed("t1", "tasks", True)
    await store.set_removed("t1", "tasks", False)
    assert await store.removed_modules("t1") == set()


async def test_enabled_and_removed_coexist_on_one_row() -> None:
    store, _ = await _fresh_store()
    await store.set_enabled("t1", "tasks", False)
    await store.set_removed("t1", "tasks", True)
    assert await store.is_enabled("t1", "tasks") is False
    assert await store.removed_modules("t1") == {"tasks"}
