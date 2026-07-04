"""Tests for the agent instructions routes (#497, ADR-0083)."""

from __future__ import annotations

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core_app.agent.instructions import (
    DEFAULT_AGENT_INSTRUCTIONS,
    AgentInstructionsStore,
)
from epicurus_core_app.agent.instructions_routes import create_instructions_router


async def _fresh_store(default: str = DEFAULT_AGENT_INSTRUCTIONS) -> AgentInstructionsStore:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    store = AgentInstructionsStore(engine, default=default)
    await store.init()
    return store


def _app(store: AgentInstructionsStore | None) -> FastAPI:
    app = FastAPI()
    app.include_router(create_instructions_router(store, default_tenant="local"))
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_get_returns_default_when_unset() -> None:
    app = _app(await _fresh_store())
    async with _client(app) as c:
        resp = await c.get("/platform/v1/agent/instructions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["instructions"] == DEFAULT_AGENT_INSTRUCTIONS
    assert body["is_default"] is True


async def test_put_persists_and_flags_not_default() -> None:
    app = _app(await _fresh_store())
    async with _client(app) as c:
        put = await c.put("/platform/v1/agent/instructions", json={"instructions": "Be terse."})
        get = await c.get("/platform/v1/agent/instructions")
    assert put.status_code == 200
    assert put.json()["is_default"] is False
    assert get.json() == {"instructions": "Be terse.", "is_default": False}


async def test_put_null_resets_to_default() -> None:
    app = _app(await _fresh_store())
    async with _client(app) as c:
        await c.put("/platform/v1/agent/instructions", json={"instructions": "Custom."})
        reset = await c.put("/platform/v1/agent/instructions", json={"instructions": None})
        get = await c.get("/platform/v1/agent/instructions")
    assert reset.json()["is_default"] is True
    assert get.json()["instructions"] == DEFAULT_AGENT_INSTRUCTIONS
    assert get.json()["is_default"] is True


async def test_put_without_store_is_503() -> None:
    app = _app(None)
    async with _client(app) as c:
        resp = await c.put("/platform/v1/agent/instructions", json={"instructions": "x"})
    assert resp.status_code == 503


async def test_get_without_store_returns_shipped_default() -> None:
    app = _app(None)
    async with _client(app) as c:
        resp = await c.get("/platform/v1/agent/instructions")
    assert resp.json()["instructions"] == DEFAULT_AGENT_INSTRUCTIONS
    assert resp.json()["is_default"] is True


async def test_tenant_id_isolates_reads_and_writes() -> None:
    """A ``tenant_id`` query param scopes the route to that tenant (mirrors timezone, #447)."""
    app = _app(await _fresh_store())
    async with _client(app) as c:
        put = await c.put(
            "/platform/v1/agent/instructions",
            params={"tenant_id": "b"},
            json={"instructions": "B prompt."},
        )
        default_get = await c.get("/platform/v1/agent/instructions")
        tenant_b_get = await c.get("/platform/v1/agent/instructions", params={"tenant_id": "b"})
    assert put.status_code == 200
    assert default_get.json()["is_default"] is True  # default tenant untouched
    assert tenant_b_get.json() == {"instructions": "B prompt.", "is_default": False}
