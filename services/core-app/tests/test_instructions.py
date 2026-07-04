"""Unit tests for AgentInstructionsStore (in-memory SQLite, StaticPool) (#497)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core_app.agent.instructions import (
    DEFAULT_AGENT_INSTRUCTIONS,
    AgentInstructionsStore,
)


async def _fresh(default: str = DEFAULT_AGENT_INSTRUCTIONS) -> AgentInstructionsStore:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    store = AgentInstructionsStore(engine, default=default)
    await store.init()
    return store


async def test_defaults_to_shipped_default() -> None:
    store = await _fresh()
    assert await store.get_instructions("t1") == DEFAULT_AGENT_INSTRUCTIONS
    assert await store.get_raw("t1") is None  # unset → route flags is_default


async def test_set_and_get() -> None:
    store = await _fresh()
    await store.set_instructions("t1", "Be terse.")
    assert await store.get_instructions("t1") == "Be terse."
    assert await store.get_raw("t1") == "Be terse."


async def test_set_is_tenant_scoped() -> None:
    store = await _fresh()
    await store.set_instructions("t1", "Be terse.")
    assert await store.get_instructions("t2") == DEFAULT_AGENT_INSTRUCTIONS


async def test_blank_resets_to_default() -> None:
    store = await _fresh()
    await store.set_instructions("t1", "Custom.")
    await store.set_instructions("t1", "   ")  # a blank body clears the override
    assert await store.get_raw("t1") is None
    assert await store.get_instructions("t1") == DEFAULT_AGENT_INSTRUCTIONS


async def test_none_resets_to_default() -> None:
    store = await _fresh()
    await store.set_instructions("t1", "Custom.")
    await store.set_instructions("t1", None)
    assert await store.get_raw("t1") is None


async def test_value_is_outer_stripped() -> None:
    store = await _fresh()
    await store.set_instructions("t1", "  Be terse.\n  ")
    assert await store.get_raw("t1") == "Be terse."


async def test_custom_default_is_used() -> None:
    store = await _fresh(default="Fallback prompt.")
    assert await store.get_instructions("t1") == "Fallback prompt."


async def test_init_heals_legacy_table_without_instructions_column() -> None:
    """A pre-existing table missing ``instructions`` is migrated in place (mirrors llm_prefs)."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE agent_instructions (tenant VARCHAR(63) PRIMARY KEY)"
        )
        await conn.exec_driver_sql("INSERT INTO agent_instructions (tenant) VALUES ('t1')")
    store = AgentInstructionsStore(engine, default="D")
    await store.init()  # must ADD COLUMN instructions rather than fail
    assert await store.get_instructions("t1") == "D"
    await store.set_instructions("t1", "X")
    assert await store.get_instructions("t1") == "X"
