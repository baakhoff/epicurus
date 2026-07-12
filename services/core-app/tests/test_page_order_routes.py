"""Tests for the page-order routes (#543)."""

from __future__ import annotations

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core_app.page_order_prefs import PageOrderStore
from epicurus_core_app.page_order_routes import create_page_order_router


async def _fresh_prefs() -> PageOrderStore:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    prefs = PageOrderStore(engine)
    await prefs.init()
    return prefs


def _app(prefs: PageOrderStore | None) -> FastAPI:
    app = FastAPI()
    app.include_router(create_page_order_router(prefs, default_tenant="local"))
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_get_returns_empty_when_unset() -> None:
    app = _app(await _fresh_prefs())
    async with _client(app) as c:
        resp = await c.get("/platform/v1/page-order")
    assert resp.status_code == 200
    assert resp.json() == {"order": []}


async def test_put_then_get_round_trips() -> None:
    app = _app(await _fresh_prefs())
    async with _client(app) as c:
        put = await c.put(
            "/platform/v1/page-order", json={"order": ["calendar/main", "tasks/board"]}
        )
        get = await c.get("/platform/v1/page-order")
    assert put.status_code == 200
    assert put.json() == {"order": ["calendar/main", "tasks/board"]}
    assert get.json() == {"order": ["calendar/main", "tasks/board"]}


async def test_put_empty_list_is_accepted_and_clears() -> None:
    app = _app(await _fresh_prefs())
    async with _client(app) as c:
        await c.put("/platform/v1/page-order", json={"order": ["a", "b"]})
        put = await c.put("/platform/v1/page-order", json={"order": []})
        get = await c.get("/platform/v1/page-order")
    assert put.status_code == 200
    assert get.json() == {"order": []}


async def test_put_without_store_is_503() -> None:
    app = _app(None)
    async with _client(app) as c:
        resp = await c.put("/platform/v1/page-order", json={"order": ["a"]})
    assert resp.status_code == 503


async def test_get_without_store_returns_empty_not_error() -> None:
    """No store configured degrades to 'no preference', not a hard failure (matches timezone's
    own no-store GET behavior — the nav still renders the manifest-default order)."""
    app = _app(None)
    async with _client(app) as c:
        resp = await c.get("/platform/v1/page-order")
    assert resp.status_code == 200
    assert resp.json() == {"order": []}


async def test_tenant_id_isolates_reads_and_writes() -> None:
    app = _app(await _fresh_prefs())
    async with _client(app) as c:
        put = await c.put(
            "/platform/v1/page-order",
            params={"tenant_id": "b"},
            json={"order": ["x", "y"]},
        )
        default_get = await c.get("/platform/v1/page-order")
        tenant_b_get = await c.get("/platform/v1/page-order", params={"tenant_id": "b"})
    assert put.status_code == 200
    assert default_get.json() == {"order": []}
    assert tenant_b_get.json() == {"order": ["x", "y"]}
