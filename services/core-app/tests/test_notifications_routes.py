"""Tests for the notification-center routes: list/filter, unread-count, mark read."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core_app.notifications import NotificationStore
from epicurus_core_app.notifications_routes import create_notifications_router

TENANT = "test"


async def _fresh_store() -> NotificationStore:
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    store = NotificationStore(engine)
    await store.init()
    return store


def _app(store: NotificationStore) -> FastAPI:
    app = FastAPI()
    app.include_router(create_notifications_router(store, default_tenant=TENANT))
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_list_returns_notifications_newest_first() -> None:
    store = await _fresh_store()
    await store.create(tenant=TENANT, category="mail", title="first", body="b")
    await store.create(tenant=TENANT, category="tasks", title="second", body="b")
    app = _app(store)
    async with _client(app) as c:
        resp = await c.get("/platform/v1/notifications")
    assert resp.status_code == 200
    assert [n["title"] for n in resp.json()] == ["second", "first"]


async def test_list_filters_by_category_query_param() -> None:
    store = await _fresh_store()
    await store.create(tenant=TENANT, category="mail", title="m", body="b")
    await store.create(tenant=TENANT, category="tasks", title="t", body="b")
    app = _app(store)
    async with _client(app) as c:
        resp = await c.get("/platform/v1/notifications", params={"category": "mail"})
    assert [n["title"] for n in resp.json()] == ["m"]


async def test_list_filters_unread_only_query_param() -> None:
    store = await _fresh_store()
    a = await store.create(tenant=TENANT, category="mail", title="a", body="b")
    await store.create(tenant=TENANT, category="mail", title="b", body="b")
    await store.mark_read(tenant=TENANT, notification_id=a.id)
    app = _app(store)
    async with _client(app) as c:
        resp = await c.get("/platform/v1/notifications", params={"unread_only": True})
    assert [n["title"] for n in resp.json()] == ["b"]


async def test_list_round_trips_entity_ref_and_deep_link() -> None:
    store = await _fresh_store()
    ref = {"ref_id": "e1", "module": "mail", "kind": "thread", "title": "Hello"}
    await store.create(
        tenant=TENANT,
        category="mail",
        title="t",
        body="b",
        deep_link="/m/mail/e1",
        entity_ref=ref,
    )
    app = _app(store)
    async with _client(app) as c:
        resp = await c.get("/platform/v1/notifications")
    body = resp.json()[0]
    assert body["entity_ref"] == ref
    assert body["deep_link"] == "/m/mail/e1"


async def test_unread_count_endpoint() -> None:
    store = await _fresh_store()
    await store.create(tenant=TENANT, category="mail", title="a", body="b")
    await store.create(tenant=TENANT, category="mail", title="b", body="b")
    app = _app(store)
    async with _client(app) as c:
        resp = await c.get("/platform/v1/notifications/unread-count")
    assert resp.json() == {"count": 2}


async def test_mark_read_endpoint() -> None:
    store = await _fresh_store()
    n = await store.create(tenant=TENANT, category="mail", title="a", body="b")
    app = _app(store)
    async with _client(app) as c:
        mark = await c.post(f"/platform/v1/notifications/{n.id}/read")
        count = await c.get("/platform/v1/notifications/unread-count")
    assert mark.status_code == 204
    assert count.json() == {"count": 0}


async def test_mark_read_404s_an_unknown_id() -> None:
    store = await _fresh_store()
    app = _app(store)
    async with _client(app) as c:
        resp = await c.post("/platform/v1/notifications/nope/read")
    assert resp.status_code == 404


async def test_mark_all_read_endpoint() -> None:
    store = await _fresh_store()
    await store.create(tenant=TENANT, category="mail", title="a", body="b")
    await store.create(tenant=TENANT, category="tasks", title="b", body="b")
    app = _app(store)
    async with _client(app) as c:
        resp = await c.post("/platform/v1/notifications/read-all")
        count = await c.get("/platform/v1/notifications/unread-count")
    assert resp.json() == {"marked": 2}
    assert count.json() == {"count": 0}


async def test_tenant_id_isolates_list_and_unread_count() -> None:
    store = await _fresh_store()
    app = _app(store)
    # Created through the store directly, scoped to tenant "b" — there is no create
    # route (writes happen only via PushService.notify).
    await store.create(tenant="b", category="mail", title="theirs", body="x")
    async with _client(app) as c:
        default_list = await c.get("/platform/v1/notifications")
        b_list = await c.get("/platform/v1/notifications", params={"tenant_id": "b"})
        default_count = await c.get("/platform/v1/notifications/unread-count")
        b_count = await c.get("/platform/v1/notifications/unread-count", params={"tenant_id": "b"})
    assert default_list.json() == []
    assert len(b_list.json()) == 1
    assert default_count.json() == {"count": 0}
    assert b_count.json() == {"count": 1}


async def test_response_shape_matches_notification_view() -> None:
    store = await _fresh_store()
    await store.create(
        tenant=TENANT, category="automation", title="t", body="b", automation_id="auto-1"
    )
    app = _app(store)
    async with _client(app) as c:
        resp = await c.get("/platform/v1/notifications")
    body: dict[str, Any] = resp.json()[0]
    assert set(body) == {
        "id",
        "category",
        "title",
        "body",
        "deep_link",
        "entity_ref",
        "automation_id",
        "created_at",
        "read_at",
    }
    assert body["automation_id"] == "auto-1"
    assert body["read_at"] is None
