"""Tests for SessionModelStore — a session's persisted model override (#707)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core_app.agent.session_model import SessionModelStore

TENANT = "local"


async def _fresh() -> SessionModelStore:
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    store = SessionModelStore(engine)
    await store.init()
    return store


async def test_a_session_with_no_override_has_none() -> None:
    store = await _fresh()
    assert await store.get(tenant=TENANT, session_id="s1") is None


async def test_set_then_get_round_trips() -> None:
    store = await _fresh()
    await store.set(tenant=TENANT, session_id="s1", model="qwen2.5:7b")
    assert await store.get(tenant=TENANT, session_id="s1") == "qwen2.5:7b"


async def test_setting_again_overwrites_not_duplicates() -> None:
    # The one write both the tool and an explicit picker change use — whichever happens
    # last must simply win, never error on a re-set.
    store = await _fresh()
    await store.set(tenant=TENANT, session_id="s1", model="qwen2.5:7b")
    await store.set(tenant=TENANT, session_id="s1", model="grok/grok-4.5-latest")
    assert await store.get(tenant=TENANT, session_id="s1") == "grok/grok-4.5-latest"


async def test_get_is_tenant_scoped() -> None:
    store = await _fresh()
    await store.set(tenant=TENANT, session_id="s1", model="qwen2.5:7b")
    assert await store.get(tenant="other", session_id="s1") is None


async def test_lookup_maps_only_sessions_with_an_override() -> None:
    store = await _fresh()
    await store.set(tenant=TENANT, session_id="s1", model="qwen2.5:7b")
    result = await store.lookup(tenant=TENANT, session_ids=["s1", "s2"])
    assert result == {"s1": "qwen2.5:7b"}


async def test_lookup_is_tenant_scoped() -> None:
    store = await _fresh()
    await store.set(tenant="other", session_id="s1", model="qwen2.5:7b")
    assert await store.lookup(tenant=TENANT, session_ids=["s1"]) == {}


async def test_lookup_of_no_session_ids_is_a_no_op() -> None:
    store = await _fresh()
    assert await store.lookup(tenant=TENANT, session_ids=[]) == {}


async def test_clear_drops_the_override() -> None:
    store = await _fresh()
    await store.set(tenant=TENANT, session_id="s1", model="qwen2.5:7b")
    await store.clear(tenant=TENANT, session_id="s1")
    assert await store.get(tenant=TENANT, session_id="s1") is None


async def test_clearing_an_unset_session_is_a_no_op() -> None:
    store = await _fresh()
    await store.clear(tenant=TENANT, session_id="never-set")  # must not raise
    assert await store.get(tenant=TENANT, session_id="never-set") is None
