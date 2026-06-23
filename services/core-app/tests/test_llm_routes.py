"""Tests for the LLM gateway router after the chat-surface cleanup (#114)."""

from __future__ import annotations

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core_app.llm.catalog import CatalogEntry, ModelCatalog
from epicurus_core_app.llm.prefs import LlmPrefsStore
from epicurus_core_app.llm.routes import create_llm_router


class _StubGateway:
    """Only needs to exist — these tests inspect routes, not call behavior."""


async def _fresh_prefs() -> LlmPrefsStore:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    prefs = LlmPrefsStore(engine)
    await prefs.init()
    return prefs


def _app(prefs: LlmPrefsStore | None = None, catalog: ModelCatalog | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_llm_router(
            _StubGateway(),  # type: ignore[arg-type]
            prefs=prefs,
            default_tenant="local",
            catalog=catalog,
        )
    )
    return app


def test_llm_chat_endpoint_is_removed() -> None:
    # Folded into POST /platform/v1/chat (ADR-0021); the gateway no longer serves it.
    assert "/platform/v1/llm/chat" not in _app().openapi()["paths"]


def test_management_routes_remain() -> None:
    paths = _app().openapi()["paths"]
    assert "/platform/v1/llm/models" in paths
    assert "/platform/v1/llm/providers" in paths
    assert "/platform/v1/llm/pull" in paths
    assert "/platform/v1/llm/catalog" in paths


async def test_catalog_route_without_catalog_returns_empty_stale() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(catalog=None)), base_url="http://test"
    ) as client:
        resp = await client.get("/platform/v1/llm/catalog")
    assert resp.status_code == 200
    body = resp.json()
    assert body["entries"] == []
    assert body["stale"] is True


async def test_catalog_route_serves_the_snapshot() -> None:
    seed = [CatalogEntry(id="llama3.2:3b", family="llama3.2", params="3b", tags=["general"])]
    catalog = ModelCatalog(
        source_url="http://example/library", refresh_seconds=3600, enabled=False, seed=seed
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(catalog=catalog)), base_url="http://test"
    ) as client:
        resp = await client.get("/platform/v1/llm/catalog")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "http://example/library"
    assert [e["id"] for e in body["entries"]] == ["llama3.2:3b"]
    assert body["entries"][0]["tags"] == ["general"]


def test_prefs_routes_present() -> None:
    paths = _app().openapi()["paths"]
    assert "/platform/v1/llm/prefs" in paths
    assert "/platform/v1/llm/prefs/default" in paths
    assert "/platform/v1/llm/prefs/embed-default" in paths
    assert "/platform/v1/llm/prefs/context-window" in paths
    assert "/platform/v1/llm/prefs/hidden" in paths


async def test_llm_chat_post_returns_404() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/platform/v1/llm/chat", json={"messages": [{"role": "user", "content": "hi"}]}
        )
    assert resp.status_code == 404


async def test_prefs_returns_empty_defaults_without_prefs_store() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(prefs=None)), base_url="http://test"
    ) as client:
        resp = await client.get("/platform/v1/llm/prefs")
    assert resp.status_code == 200
    data = resp.json()
    assert data["global_default"] is None
    assert data["global_embed_default"] is None
    assert data["hidden"] == []


async def test_prefs_set_and_get_default() -> None:
    prefs = await _fresh_prefs()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(prefs=prefs)), base_url="http://test"
    ) as client:
        put = await client.put("/platform/v1/llm/prefs/default", json={"model": "qwen2.5:7b"})
        assert put.status_code == 200
        get = await client.get("/platform/v1/llm/prefs")
    assert get.json()["global_default"] == "qwen2.5:7b"


async def test_prefs_clear_default() -> None:
    prefs = await _fresh_prefs()
    await prefs.set_default("local", "qwen2.5:7b")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(prefs=prefs)), base_url="http://test"
    ) as client:
        await client.put("/platform/v1/llm/prefs/default", json={"model": None})
        get = await client.get("/platform/v1/llm/prefs")
    assert get.json()["global_default"] is None


async def test_prefs_toggle_hidden() -> None:
    prefs = await _fresh_prefs()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(prefs=prefs)), base_url="http://test"
    ) as client:
        # Hide phi3:mini
        resp = await client.put(
            "/platform/v1/llm/prefs/hidden", json={"name": "phi3:mini", "hidden": True}
        )
        assert resp.status_code == 200
        assert "phi3:mini" in resp.json()["hidden"]

        # Verify GET reflects it
        get = await client.get("/platform/v1/llm/prefs")
        assert "phi3:mini" in get.json()["hidden"]

        # Unhide it
        resp2 = await client.put(
            "/platform/v1/llm/prefs/hidden", json={"name": "phi3:mini", "hidden": False}
        )
        assert "phi3:mini" not in resp2.json()["hidden"]


async def test_prefs_hidden_no_duplicates() -> None:
    prefs = await _fresh_prefs()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(prefs=prefs)), base_url="http://test"
    ) as client:
        await client.put(
            "/platform/v1/llm/prefs/hidden", json={"name": "phi3:mini", "hidden": True}
        )
        # Second hide must not duplicate the entry
        resp = await client.put(
            "/platform/v1/llm/prefs/hidden", json={"name": "phi3:mini", "hidden": True}
        )
    assert resp.json()["hidden"].count("phi3:mini") == 1


# ── Embedding default preference ──────────────────────────────────────────────


async def test_prefs_embed_default_initially_null() -> None:
    prefs = await _fresh_prefs()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(prefs=prefs)), base_url="http://test"
    ) as client:
        resp = await client.get("/platform/v1/llm/prefs")
    assert resp.json()["global_embed_default"] is None


async def test_prefs_set_and_get_embed_default() -> None:
    prefs = await _fresh_prefs()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(prefs=prefs)), base_url="http://test"
    ) as client:
        put = await client.put(
            "/platform/v1/llm/prefs/embed-default", json={"model": "nomic-embed-text"}
        )
        assert put.status_code == 200
        get = await client.get("/platform/v1/llm/prefs")
    assert get.json()["global_embed_default"] == "nomic-embed-text"


async def test_prefs_clear_embed_default() -> None:
    prefs = await _fresh_prefs()
    await prefs.set_embed_default("local", "nomic-embed-text")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(prefs=prefs)), base_url="http://test"
    ) as client:
        await client.put("/platform/v1/llm/prefs/embed-default", json={"model": None})
        get = await client.get("/platform/v1/llm/prefs")
    assert get.json()["global_embed_default"] is None


async def test_prefs_embed_default_no_store_returns_503() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(prefs=None)), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/platform/v1/llm/prefs/embed-default", json={"model": "nomic-embed-text"}
        )
    assert resp.status_code == 503


# ── Context-window preference ─────────────────────────────────────────────────


async def test_prefs_context_window_initially_null() -> None:
    prefs = await _fresh_prefs()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(prefs=prefs)), base_url="http://test"
    ) as client:
        resp = await client.get("/platform/v1/llm/prefs")
    assert resp.json()["global_context_window"] is None


async def test_prefs_set_and_get_context_window() -> None:
    prefs = await _fresh_prefs()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(prefs=prefs)), base_url="http://test"
    ) as client:
        put = await client.put("/platform/v1/llm/prefs/context-window", json={"value": 16384})
        assert put.status_code == 200
        get = await client.get("/platform/v1/llm/prefs")
    assert get.json()["global_context_window"] == 16384


async def test_prefs_clear_context_window() -> None:
    prefs = await _fresh_prefs()
    await prefs.set_context_window("local", 16384)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(prefs=prefs)), base_url="http://test"
    ) as client:
        await client.put("/platform/v1/llm/prefs/context-window", json={"value": None})
        get = await client.get("/platform/v1/llm/prefs")
    assert get.json()["global_context_window"] is None


async def test_prefs_context_window_no_store_returns_503() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(prefs=None)), base_url="http://test"
    ) as client:
        resp = await client.put("/platform/v1/llm/prefs/context-window", json={"value": 8192})
    assert resp.status_code == 503


async def test_agent_max_steps_route_clamps_and_round_trips() -> None:
    prefs = await _fresh_prefs()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(prefs=prefs)), base_url="http://test"
    ) as client:
        # Over the ceiling → clamped to 12.
        put = await client.put("/platform/v1/llm/prefs/agent-max-steps", json={"value": 99})
        assert put.status_code == 200
        assert put.json()["value"] == 12
        got = await client.get("/platform/v1/llm/prefs")
    assert got.json()["global_agent_max_steps"] == 12


async def test_agent_max_steps_floor_and_clear() -> None:
    prefs = await _fresh_prefs()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(prefs=prefs)), base_url="http://test"
    ) as client:
        floored = await client.put("/platform/v1/llm/prefs/agent-max-steps", json={"value": 0})
        assert floored.json()["value"] == 1  # clamped up to the floor
        cleared = await client.put("/platform/v1/llm/prefs/agent-max-steps", json={"value": None})
        assert cleared.json()["value"] is None
        got = await client.get("/platform/v1/llm/prefs")
    assert got.json()["global_agent_max_steps"] is None
