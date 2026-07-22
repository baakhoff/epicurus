"""Unit tests for PushSubscriptionStore (in-memory SQLite, StaticPool)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core_app.push.subscriptions import PushSubscriptionStore

TENANT = "t1"


async def _store() -> PushSubscriptionStore:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    store = PushSubscriptionStore(engine)
    await store.init()
    return store


async def test_create_returns_a_new_subscription() -> None:
    store = await _store()
    sub = await store.create_or_update(
        tenant=TENANT,
        endpoint="https://push.example/abc",
        p256dh="p",
        auth="a",
        device_label="Pixel 8",
    )
    assert sub.id and len(sub.id) == 32  # uuid4 hex
    assert sub.endpoint == "https://push.example/abc"
    assert sub.device_label == "Pixel 8"
    assert sub.last_seen_at is not None


async def test_re_subscribing_the_same_endpoint_upserts_not_duplicates() -> None:
    store = await _store()
    first = await store.create_or_update(
        tenant=TENANT, endpoint="https://push.example/abc", p256dh="p1", auth="a1"
    )
    second = await store.create_or_update(
        tenant=TENANT, endpoint="https://push.example/abc", p256dh="p2", auth="a2"
    )
    assert first.id == second.id
    rows = await store.list(TENANT)
    assert len(rows) == 1
    assert rows[0].p256dh == "p2" and rows[0].auth == "a2"


async def test_blank_device_label_does_not_clear_an_existing_one() -> None:
    store = await _store()
    await store.create_or_update(
        tenant=TENANT,
        endpoint="https://push.example/abc",
        p256dh="p",
        auth="a",
        device_label="Pixel 8",
    )
    updated = await store.create_or_update(
        tenant=TENANT, endpoint="https://push.example/abc", p256dh="p2", auth="a2"
    )
    assert updated.device_label == "Pixel 8"  # blank label on refresh keeps the prior one


async def test_list_is_tenant_scoped_and_ordered() -> None:
    store = await _store()
    await store.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    await store.create_or_update(tenant=TENANT, endpoint="e2", p256dh="p", auth="a")
    await store.create_or_update(tenant="other", endpoint="e3", p256dh="p", auth="a")
    rows = await store.list(TENANT)
    assert [r.endpoint for r in rows] == ["e1", "e2"]
    assert len(await store.list("other")) == 1


async def test_delete_removes_and_reports_unknown() -> None:
    store = await _store()
    sub = await store.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    assert await store.delete(tenant=TENANT, sub_id=sub.id) is True
    assert await store.list(TENANT) == []
    assert await store.delete(tenant=TENANT, sub_id=sub.id) is False  # already gone


async def test_delete_is_tenant_scoped() -> None:
    store = await _store()
    sub = await store.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    assert await store.delete(tenant="other", sub_id=sub.id) is False  # wrong tenant
    assert len(await store.list(TENANT)) == 1


async def test_delete_by_endpoint_prunes_a_gone_subscription() -> None:
    store = await _store()
    await store.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    assert await store.delete_by_endpoint(tenant=TENANT, endpoint="e1") is True
    assert await store.list(TENANT) == []
    assert await store.delete_by_endpoint(tenant=TENANT, endpoint="e1") is False
