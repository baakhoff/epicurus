"""Unit tests for EventSubscriptionStore — the (tenant, module, event_type) -> ChannelPrefs
overlay behind per-event alerts (#732)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core_app.push.event_subscriptions import EventSubscription, EventSubscriptionStore
from epicurus_core_app.push.prefs import ChannelPrefs

TENANT = "t1"


async def _store() -> EventSubscriptionStore:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    store = EventSubscriptionStore(engine)
    await store.init()
    return store


async def test_get_on_an_unsubscribed_event_returns_none() -> None:
    """Unlike PushPrefs.categories, the default is off — no row, not ChannelPrefs()."""
    store = await _store()
    prefs = await store.get(TENANT, module="mail", event_type="mail.received")
    assert prefs is None


async def test_list_on_a_fresh_tenant_is_empty() -> None:
    store = await _store()
    assert await store.list(TENANT) == []


async def test_set_then_get_round_trips() -> None:
    store = await _store()
    await store.set(
        TENANT,
        module="mail",
        event_type="mail.received",
        prefs=ChannelPrefs(push=True, center=False),
    )
    prefs = await store.get(TENANT, module="mail", event_type="mail.received")
    assert prefs == ChannelPrefs(push=True, center=False)


async def test_set_twice_upserts_rather_than_duplicating() -> None:
    store = await _store()
    await store.set(
        TENANT,
        module="mail",
        event_type="mail.received",
        prefs=ChannelPrefs(push=True, center=True),
    )
    await store.set(
        TENANT,
        module="mail",
        event_type="mail.received",
        prefs=ChannelPrefs(push=False, center=True),
    )
    prefs = await store.get(TENANT, module="mail", event_type="mail.received")
    assert prefs == ChannelPrefs(push=False, center=True)
    assert await store.list(TENANT) == [
        EventSubscription(module="mail", event_type="mail.received", push=False, center=True)
    ]


async def test_set_with_both_channels_off_deletes_the_row() -> None:
    """Off-by-default means "both false" and "no row" must collapse to the same state."""
    store = await _store()
    await store.set(
        TENANT,
        module="mail",
        event_type="mail.received",
        prefs=ChannelPrefs(push=True, center=True),
    )
    await store.set(
        TENANT,
        module="mail",
        event_type="mail.received",
        prefs=ChannelPrefs(push=False, center=False),
    )
    assert await store.get(TENANT, module="mail", event_type="mail.received") is None
    assert await store.list(TENANT) == []


async def test_set_with_both_off_on_a_never_subscribed_event_is_a_no_op() -> None:
    store = await _store()
    await store.set(
        TENANT,
        module="mail",
        event_type="mail.received",
        prefs=ChannelPrefs(push=False, center=False),
    )
    assert await store.list(TENANT) == []


async def test_list_returns_only_this_tenants_rows() -> None:
    store = await _store()
    await store.set(
        TENANT,
        module="mail",
        event_type="mail.received",
        prefs=ChannelPrefs(push=True, center=True),
    )
    await store.set(
        "other",
        module="mail",
        event_type="mail.received",
        prefs=ChannelPrefs(push=True, center=True),
    )
    assert [s.module for s in await store.list(TENANT)] == ["mail"]
    assert await store.get("other", module="mail", event_type="mail.received") is not None


async def test_list_is_sorted_by_module_then_event_type() -> None:
    store = await _store()
    await store.set(
        TENANT, module="tasks", event_type="tasks.due", prefs=ChannelPrefs(push=True, center=True)
    )
    await store.set(
        TENANT,
        module="mail",
        event_type="mail.received",
        prefs=ChannelPrefs(push=True, center=True),
    )
    await store.set(
        TENANT, module="mail", event_type="mail.sent", prefs=ChannelPrefs(push=True, center=True)
    )
    rows = await store.list(TENANT)
    assert [(s.module, s.event_type) for s in rows] == [
        ("mail", "mail.received"),
        ("mail", "mail.sent"),
        ("tasks", "tasks.due"),
    ]


async def test_distinct_event_types_on_the_same_module_are_independent() -> None:
    store = await _store()
    await store.set(
        TENANT,
        module="mail",
        event_type="mail.received",
        prefs=ChannelPrefs(push=True, center=True),
    )
    assert await store.get(TENANT, module="mail", event_type="mail.sent") is None
