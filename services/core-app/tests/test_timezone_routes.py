"""Tests for the timezone routes (ADR-0039)."""

from __future__ import annotations

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core_app.timezone_prefs import TimezonePrefsStore
from epicurus_core_app.timezone_routes import create_timezone_router


async def _fresh_prefs(default: str = "UTC") -> TimezonePrefsStore:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    prefs = TimezonePrefsStore(engine, default=default)
    await prefs.init()
    return prefs


def _app(prefs: TimezonePrefsStore | None) -> FastAPI:
    app = FastAPI()
    app.include_router(create_timezone_router(prefs, default_tenant="local"))
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_get_returns_default_when_unset() -> None:
    app = _app(await _fresh_prefs(default="Europe/Belgrade"))
    async with _client(app) as c:
        resp = await c.get("/platform/v1/timezone")
    assert resp.status_code == 200
    assert resp.json() == {"timezone": "Europe/Belgrade"}


async def test_put_valid_persists() -> None:
    app = _app(await _fresh_prefs())
    async with _client(app) as c:
        put = await c.put("/platform/v1/timezone", json={"timezone": "Asia/Tokyo"})
        get = await c.get("/platform/v1/timezone")
    assert put.status_code == 200
    assert get.json() == {"timezone": "Asia/Tokyo"}


async def test_put_invalid_timezone_is_400() -> None:
    app = _app(await _fresh_prefs())
    async with _client(app) as c:
        resp = await c.put("/platform/v1/timezone", json={"timezone": "Not/AZone"})
    assert resp.status_code == 400


async def test_put_without_store_is_503() -> None:
    app = _app(None)
    async with _client(app) as c:
        resp = await c.put("/platform/v1/timezone", json={"timezone": "UTC"})
    assert resp.status_code == 503
