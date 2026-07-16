"""Unit tests for PlaybookStore — CRUD, ADR-0046 versioning, composition (ADR-0093 §3/§4)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core_app.agent.playbooks import MAX_VERSIONS, PlaybookStore


def _engine() -> AsyncEngine:
    return create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


async def _fresh() -> PlaybookStore:
    store = PlaybookStore(_engine())
    await store.init()
    return store


async def test_create_and_list() -> None:
    store = await _fresh()
    made = await store.create("t1", name="Morning briefing", content="Calendar before mail.")
    assert made.enabled is True
    assert [p.name for p in await store.list_playbooks("t1")] == ["Morning briefing"]


async def test_list_is_tenant_scoped() -> None:
    store = await _fresh()
    await store.create("t1", name="Mine", content="x")
    assert await store.list_playbooks("t2") == []


async def test_get_is_tenant_scoped() -> None:
    """Another tenant's id resolves to None rather than leaking across the boundary."""
    store = await _fresh()
    made = await store.create("t1", name="Mine", content="x")
    assert await store.get("t1", made.id) is not None
    assert await store.get("t2", made.id) is None


async def test_get_by_name() -> None:
    store = await _fresh()
    await store.create("t1", name="Morning briefing", content="x")
    found = await store.get_by_name("t1", "Morning briefing")
    assert found is not None and found.content == "x"
    assert await store.get_by_name("t1", "Nope") is None
    assert await store.get_by_name("t2", "Morning briefing") is None


async def test_save_updates_content_and_snapshots_the_previous_body() -> None:
    """ADR-0046: the save snapshots what was there *before*, so the edit is undoable."""
    store = await _fresh()
    made = await store.create("t1", name="P", content="v1")
    updated = await store.save("t1", made.id, content="v2")
    assert updated is not None and updated.content == "v2"

    versions = await store.versions("t1", made.id)
    assert [v.size for v in versions] == [len("v1")]
    body = await store.version("t1", versions[0].version_id)
    assert body is not None and body.content == "v1"


async def test_create_snapshots_nothing() -> None:
    """A create has no prior body, so it records no version (the editor's own rule)."""
    store = await _fresh()
    made = await store.create("t1", name="P", content="v1")
    assert await store.versions("t1", made.id) == []


async def test_save_of_unknown_playbook_returns_none() -> None:
    store = await _fresh()
    assert await store.save("t1", "nope", content="x") is None


async def test_repeated_identical_saves_record_nothing() -> None:
    """A no-op save must not pile up snapshots (ADR-0046's dedup rule).

    Nothing was replaced, so there is nothing to undo back to — a save that changes no bytes
    records no version at all, and a later real edit still snapshots the body it replaced.
    """
    store = await _fresh()
    made = await store.create("t1", name="P", content="v1")
    await store.save("t1", made.id, content="v1")
    await store.save("t1", made.id, content="v1")
    assert await store.versions("t1", made.id) == []

    await store.save("t1", made.id, content="v2")
    versions = await store.versions("t1", made.id)
    body = await store.version("t1", versions[0].version_id)
    assert body is not None and body.content == "v1"


async def test_versions_are_capped_and_oldest_pruned() -> None:
    """Retention keeps the newest MAX_VERSIONS; the oldest fall off."""
    store = await _fresh()
    made = await store.create("t1", name="P", content="v0")
    for i in range(1, MAX_VERSIONS + 6):
        await store.save("t1", made.id, content=f"v{i}")

    versions = await store.versions("t1", made.id)
    assert len(versions) == MAX_VERSIONS
    newest = await store.version("t1", versions[0].version_id)
    assert newest is not None and newest.content == f"v{MAX_VERSIONS + 4}"
    # v0..v4 were pruned: the oldest retained snapshot is v5.
    oldest = await store.version("t1", versions[-1].version_id)
    assert oldest is not None and oldest.content == "v5"


async def test_versions_are_tenant_scoped() -> None:
    store = await _fresh()
    made = await store.create("t1", name="P", content="v1")
    await store.save("t1", made.id, content="v2")
    assert await store.versions("t2", made.id) == []
    vid = (await store.versions("t1", made.id))[0].version_id
    assert await store.version("t2", vid) is None


async def test_set_enabled_toggles_without_snapshotting() -> None:
    store = await _fresh()
    made = await store.create("t1", name="P", content="v1")
    assert await store.set_enabled("t1", made.id, False) is True

    got = await store.get("t1", made.id)
    assert got is not None and got.enabled is False
    # Enabling/disabling isn't a content change, so it versions nothing.
    assert await store.versions("t1", made.id) == []


async def test_set_enabled_unknown_returns_false() -> None:
    store = await _fresh()
    assert await store.set_enabled("t1", "nope", False) is False


async def test_delete_removes_playbook_and_its_versions() -> None:
    store = await _fresh()
    made = await store.create("t1", name="P", content="v1")
    await store.save("t1", made.id, content="v2")

    assert await store.delete("t1", made.id) is True
    assert await store.get("t1", made.id) is None
    assert await store.versions("t1", made.id) == []
    assert await store.delete("t1", made.id) is False


async def test_delete_is_tenant_scoped() -> None:
    store = await _fresh()
    made = await store.create("t1", name="P", content="v1")
    assert await store.delete("t2", made.id) is False
    assert await store.get("t1", made.id) is not None


async def test_compose_renders_enabled_playbooks_under_headings() -> None:
    store = await _fresh()
    await store.create("t1", name="Briefing", content="Calendar before mail.")
    await store.create("t1", name="Filing", content="Archive read receipts.")

    composed = await store.compose("t1")
    assert "## Playbook: Briefing" in composed
    assert "Calendar before mail." in composed
    assert "## Playbook: Filing" in composed
    # Oldest first — a stable, predictable composition order.
    assert composed.index("Briefing") < composed.index("Filing")


async def test_compose_skips_disabled_playbooks() -> None:
    store = await _fresh()
    on = await store.create("t1", name="On", content="keep")
    off = await store.create("t1", name="Off", content="drop")
    await store.set_enabled("t1", off.id, False)

    composed = await store.compose("t1")
    assert "keep" in composed
    assert "drop" not in composed
    assert on.id  # the enabled one is what survived


async def test_compose_skips_blank_playbooks() -> None:
    """An enabled but empty playbook contributes no heading — no dangling section."""
    store = await _fresh()
    await store.create("t1", name="Empty", content="   ")
    assert await store.compose("t1") == ""


async def test_compose_is_empty_without_playbooks() -> None:
    """No playbooks → "" so the caller composes the base prompt byte-identically to before."""
    store = await _fresh()
    assert await store.compose("t1") == ""


async def test_compose_is_tenant_scoped() -> None:
    store = await _fresh()
    await store.create("t1", name="Mine", content="secret guidance")
    assert await store.compose("t2") == ""
