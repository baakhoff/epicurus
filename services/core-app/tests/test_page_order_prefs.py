"""Tests for the page-order preference store (#543)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core_app.page_order_prefs import PageOrderStore


async def _fresh_store() -> PageOrderStore:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    store = PageOrderStore(engine)
    await store.init()
    return store


async def test_get_returns_empty_list_when_unset() -> None:
    store = await _fresh_store()
    assert await store.get_order("t1") == []


async def test_set_then_get_round_trips() -> None:
    store = await _fresh_store()
    order = ["calendar/main", "tasks/board", "notes/vault"]
    await store.set_order("t1", order)
    assert await store.get_order("t1") == order


async def test_set_replaces_prior_order_entirely() -> None:
    store = await _fresh_store()
    await store.set_order("t1", ["a", "b", "c"])
    await store.set_order("t1", ["c", "a"])
    assert await store.get_order("t1") == ["c", "a"]


async def test_set_empty_list_clears_the_preference() -> None:
    store = await _fresh_store()
    await store.set_order("t1", ["a", "b"])
    await store.set_order("t1", [])
    assert await store.get_order("t1") == []


async def test_tenants_are_isolated() -> None:
    store = await _fresh_store()
    await store.set_order("t1", ["a", "b"])
    await store.set_order("t2", ["b", "a"])
    assert await store.get_order("t1") == ["a", "b"]
    assert await store.get_order("t2") == ["b", "a"]
