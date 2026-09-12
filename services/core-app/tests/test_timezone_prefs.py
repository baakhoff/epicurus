"""Unit tests for TimezonePrefsStore (in-memory SQLite, StaticPool)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core.db.migrations import run_migrations
from epicurus_core_app.migrations import METADATAS, SCRIPT_LOCATION, SERVICE
from epicurus_core_app.timezone_prefs import TimezonePrefsStore


async def _fresh(default: str = "UTC") -> tuple[TimezonePrefsStore, AsyncEngine]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    store = TimezonePrefsStore(engine, default=default)
    await store.init()
    return store, engine


async def test_defaults_to_configured_default() -> None:
    store, _ = await _fresh(default="Europe/Belgrade")
    assert await store.get_timezone("t1") == "Europe/Belgrade"


async def test_default_falls_back_to_utc() -> None:
    store, _ = await _fresh()
    assert await store.get_timezone("t1") == "UTC"


async def test_set_and_get() -> None:
    store, _ = await _fresh()
    await store.set_timezone("t1", "Asia/Tokyo")
    assert await store.get_timezone("t1") == "Asia/Tokyo"


async def test_set_is_tenant_scoped() -> None:
    store, _ = await _fresh(default="UTC")
    await store.set_timezone("t1", "Asia/Tokyo")
    assert await store.get_timezone("t2") == "UTC"


async def test_the_migration_heals_a_legacy_table_without_the_timezone_column() -> None:
    """A pre-existing table missing ``timezone`` is reconciled by the baseline (#834)."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE timezone_prefs (tenant VARCHAR(63) PRIMARY KEY)")
        await conn.exec_driver_sql("INSERT INTO timezone_prefs (tenant) VALUES ('t1')")
    # Must ADD COLUMN timezone, not fail.
    assert (
        await run_migrations(
            engine, service=SERVICE, script_location=SCRIPT_LOCATION, metadatas=METADATAS
        )
        == "adopted"
    )
    store = TimezonePrefsStore(engine, default="UTC")
    assert await store.get_timezone("t1") == "UTC"
    await store.set_timezone("t1", "Europe/Belgrade")
    assert await store.get_timezone("t1") == "Europe/Belgrade"
