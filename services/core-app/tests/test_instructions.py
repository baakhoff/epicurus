"""Unit tests for AgentInstructionsStore (in-memory SQLite, StaticPool) (#497)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core_app.agent.instructions import (
    DEFAULT_AGENT_INSTRUCTIONS,
    AgentInstructionsStore,
)
from epicurus_core_app.agent.playbooks import MAX_VERSIONS, PlaybookStore


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


# ── composition with playbooks + versioning (ADR-0093 §3/§4) ─────────────────


async def _with_playbooks(default: str = "BASE") -> tuple[AgentInstructionsStore, PlaybookStore]:
    """A store composing over a playbook store, both on one in-memory engine."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    playbooks = PlaybookStore(engine)
    store = AgentInstructionsStore(engine, default=default, playbooks=playbooks)
    await playbooks.init()
    await store.init()
    return store, playbooks


async def test_get_instructions_composes_enabled_playbooks_under_headings() -> None:
    store, playbooks = await _with_playbooks()
    await store.set_instructions("t1", "Be terse.")
    await playbooks.create("t1", name="Briefing", content="Calendar before mail.")

    composed = await store.get_instructions("t1")
    assert composed.startswith("Be terse.")  # the base still leads the turn
    assert "## Playbook: Briefing" in composed
    assert "Calendar before mail." in composed


async def test_get_instructions_without_playbooks_is_unchanged_behavior() -> None:
    """No playbooks → byte-identical to the pre-ADR-0093 output."""
    store, _ = await _with_playbooks()
    await store.set_instructions("t1", "Be terse.")
    assert await store.get_instructions("t1") == "Be terse."


async def test_get_instructions_composes_over_the_default_prompt_too() -> None:
    store, playbooks = await _with_playbooks()
    await playbooks.create("t1", name="P", content="guidance")

    composed = await store.get_instructions("t1")
    assert composed.startswith("BASE")
    assert "guidance" in composed


async def test_get_instructions_skips_disabled_playbooks() -> None:
    store, playbooks = await _with_playbooks()
    off = await playbooks.create("t1", name="Off", content="silenced")
    await playbooks.set_enabled("t1", off.id, False)
    assert "silenced" not in await store.get_instructions("t1")


async def test_composition_is_tenant_scoped() -> None:
    store, playbooks = await _with_playbooks()
    await playbooks.create("t1", name="Mine", content="mine only")
    assert "mine only" not in await store.get_instructions("t2")


async def test_get_base_returns_the_base_alone() -> None:
    """The review surface diffs against the editable document, never base+playbooks."""
    store, playbooks = await _with_playbooks()
    await store.set_instructions("t1", "Be terse.")
    await playbooks.create("t1", name="P", content="guidance")

    assert await store.get_base("t1") == "Be terse."
    assert "guidance" in await store.get_instructions("t1")


async def test_a_failing_playbook_read_degrades_to_the_base_prompt() -> None:
    """Enrichment must never cost the operator a turn (ADR-0094's best-effort precedent)."""

    class _Boom(PlaybookStore):
        async def compose(self, tenant: str) -> str:
            raise RuntimeError("db is down")

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    store = AgentInstructionsStore(engine, default="BASE", playbooks=_Boom(engine))
    await store.init()
    await store.set_instructions("t1", "Be terse.")
    assert await store.get_instructions("t1") == "Be terse."


async def test_set_instructions_snapshots_the_previous_prompt() -> None:
    store = await _fresh(default="D")
    await store.set_instructions("t1", "v1")
    await store.set_instructions("t1", "v2")

    versions = await store.versions("t1")
    # The first edit snapshots the shipped default, so "put it back" works from the start.
    assert [v.size for v in versions] == [len("v1"), len("D")]
    newest = await store.version("t1", versions[0].version_id)
    assert newest is not None and newest.content == "v1"


async def test_instruction_versions_dedup_and_cap() -> None:
    store = await _fresh(default="D")
    await store.set_instructions("t1", "v1")
    await store.set_instructions("t1", "v1")  # no-op → no duplicate snapshot
    assert len(await store.versions("t1")) == 1

    for i in range(2, MAX_VERSIONS + 6):
        await store.set_instructions("t1", f"v{i}")
    assert len(await store.versions("t1")) == MAX_VERSIONS


async def test_instruction_versions_are_tenant_scoped() -> None:
    store = await _fresh(default="D")
    await store.set_instructions("t1", "v1")
    assert await store.versions("t2") == []
    vid = (await store.versions("t1"))[0].version_id
    assert await store.version("t2", vid) is None


async def test_clearing_the_prompt_snapshots_it_first() -> None:
    """A reset-to-default is still an edit, so the custom prompt stays recoverable."""
    store = await _fresh(default="D")
    await store.set_instructions("t1", "custom")
    await store.set_instructions("t1", None)

    assert await store.get_instructions("t1") == "D"
    versions = await store.versions("t1")
    restored = await store.version("t1", versions[0].version_id)
    assert restored is not None and restored.content == "custom"
