"""Unit tests for ModelSettingsStore — tenant-scoped per-model tuning (context + keep-alive).

In-memory SQLite with StaticPool, same pattern as test_module_prefs.py / test_llm_prefs.py.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core_app.llm.model_settings import ModelSettings, ModelSettingsStore


async def _fresh_store() -> tuple[ModelSettingsStore, AsyncEngine]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    store = ModelSettingsStore(engine)
    await store.init()
    return store, engine


async def test_unset_model_inherits_everything() -> None:
    store, _ = await _fresh_store()
    settings = await store.get("t1", "llama3.2:latest")
    assert settings == ModelSettings()
    assert settings.is_empty()


async def test_set_and_read_round_trips() -> None:
    store, _ = await _fresh_store()
    await store.set("t1", "llama3.2:latest", ModelSettings(context_window=8192, keep_alive="30m"))
    settings = await store.get("t1", "llama3.2:latest")
    assert settings.context_window == 8192
    assert settings.keep_alive == "30m"


async def test_list_returns_only_stored_models() -> None:
    store, _ = await _fresh_store()
    await store.set("t1", "a:latest", ModelSettings(context_window=4096))
    await store.set("t1", "b:latest", ModelSettings(keep_alive="0"))
    listed = await store.list("t1")
    assert set(listed) == {"a:latest", "b:latest"}
    assert listed["a:latest"].context_window == 4096
    assert listed["b:latest"].keep_alive == "0"


async def test_empty_settings_remove_the_row() -> None:
    store, _ = await _fresh_store()
    await store.set("t1", "m:latest", ModelSettings(context_window=8192))
    await store.set("t1", "m:latest", ModelSettings())  # clear both fields
    assert await store.get("t1", "m:latest") == ModelSettings()
    assert await store.list("t1") == {}


async def test_settings_are_tenant_scoped() -> None:
    store, _ = await _fresh_store()
    await store.set("t1", "m:latest", ModelSettings(context_window=8192))
    assert (await store.get("t2", "m:latest")).is_empty()
    assert await store.list("t2") == {}


async def test_ensure_columns_heals_a_pre_existing_bare_table() -> None:
    """A table created before the columns existed gets them added in place (no migration)."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE model_settings ("
                "tenant VARCHAR(63) NOT NULL, model VARCHAR(256) NOT NULL, "
                "PRIMARY KEY (tenant, model))"
            )
        )
    store = ModelSettingsStore(engine)
    await store.init()  # must ALTER in the missing columns, not raise
    await store.set("t1", "m:latest", ModelSettings(context_window=2048, keep_alive="5m"))
    assert (await store.get("t1", "m:latest")).context_window == 2048


async def test_device_round_trips() -> None:
    store, _ = await _fresh_store()
    await store.set("t1", "llama3.2:latest", ModelSettings(device="cpu"))
    assert (await store.get("t1", "llama3.2:latest")).device == "cpu"
    # device alone is not empty (the row persists)
    assert "llama3.2:latest" in await store.list("t1")


async def test_hosted_id_round_trips_and_is_distinct_from_a_local_row() -> None:
    """A hosted id (``<provider>/<model>``) persists a context-window budget of its own (#570).

    The store is keyed by the exact id, so the hosted ``custom/llama3.2`` row is independent of a
    same-family local ``llama3.2:latest`` row — the gateway reads the hosted budget by exact id and
    never confuses the two.
    """
    store, _ = await _fresh_store()
    await store.set("t1", "llama3.2:latest", ModelSettings(context_window=2048))  # local
    await store.set("t1", "custom/llama3.2", ModelSettings(context_window=200_000))  # hosted
    assert (await store.get("t1", "custom/llama3.2")).context_window == 200_000
    assert (await store.get("t1", "llama3.2:latest")).context_window == 2048
    assert set(await store.list("t1")) == {"llama3.2:latest", "custom/llama3.2"}
