"""Tests for the LLM gateway router after the chat-surface cleanup (#114)."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core_app.llm.catalog import CatalogEntry, ModelCatalog
from epicurus_core_app.llm.model_settings import ModelSettingsStore
from epicurus_core_app.llm.models import ModelDetails
from epicurus_core_app.llm.ollama_runtime import OllamaRuntime
from epicurus_core_app.llm.prefs import LlmPrefsStore
from epicurus_core_app.llm.routes import create_llm_router
from epicurus_core_app.llm.saved_models import SavedHostedModelStore
from epicurus_core_app.llm.variants import VariantLookup


class _StubGateway:
    """Only needs to exist — most tests inspect routes, not call behavior.

    ``show`` backs the /models/details route; ``unload`` records its calls so the unload
    route can be asserted.
    """

    def __init__(self) -> None:
        self.unloaded: list[str | None] = []

    async def show(self, model: str) -> ModelDetails:
        return ModelDetails(
            quantization="Q4_K_M", parameter_size="8.0B", context_length=131072, family="llama"
        )

    async def unload(self, model: str | None = None) -> None:
        self.unloaded.append(model)


async def _fresh_prefs() -> LlmPrefsStore:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    prefs = LlmPrefsStore(engine)
    await prefs.init()
    return prefs


async def _fresh_model_settings() -> ModelSettingsStore:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    store = ModelSettingsStore(engine)
    await store.init()
    return store


async def _fresh_saved_models() -> SavedHostedModelStore:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    store = SavedHostedModelStore(engine)
    await store.init()
    return store


def _app(
    prefs: LlmPrefsStore | None = None,
    catalog: ModelCatalog | None = None,
    variants: VariantLookup | None = None,
    model_settings: ModelSettingsStore | None = None,
    ollama_runtime: OllamaRuntime | None = None,
    gateway: _StubGateway | None = None,
    saved_models: SavedHostedModelStore | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_llm_router(
            gateway or _StubGateway(),  # type: ignore[arg-type]
            prefs=prefs,
            default_tenant="local",
            catalog=catalog,
            variants=variants,
            model_settings=model_settings,
            ollama_runtime=ollama_runtime,
            saved_models=saved_models,
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
    assert "/platform/v1/llm/unload" in paths


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


async def test_unload_route_calls_gateway_without_power_change() -> None:
    gateway = _StubGateway()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(gateway=gateway)), base_url="http://test"
    ) as client:
        all_resp = await client.post("/platform/v1/llm/unload", json={})
        one_resp = await client.post("/platform/v1/llm/unload", json={"model": "llama3.1:8b"})
    assert all_resp.status_code == 200
    assert all_resp.json()["model"] == "all"
    assert one_resp.json()["model"] == "llama3.1:8b"
    # The route delegates to gateway.unload(model) — None (all) then the named one.
    assert gateway.unloaded == [None, "llama3.1:8b"]


async def test_variants_route_without_lookup_returns_empty() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(variants=None)), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/platform/v1/llm/catalog/variants", params={"model": "llama3.1:8b"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["model"] == "llama3.1:8b"
    assert body["variants"] == []


async def test_variants_route_serves_the_lookup() -> None:
    async def fetch(url: str) -> str:
        return (
            '<a href="/library/llama3.1:latest"></a>'
            '<a href="/library/llama3.1:8b"></a>'
            '<a href="/library/llama3.1:8b-instruct-q8_0"></a>'
            '<a href="/library/llama3.1:70b"></a>'
        )

    lookup = VariantLookup(library_url="http://lib.example/library", fetch=fetch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(variants=lookup)), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/platform/v1/llm/catalog/variants", params={"model": "llama3.1:8b"}
        )
    assert resp.status_code == 200
    tags = [v["tag"] for v in resp.json()["variants"]]
    assert "llama3.1:8b-instruct-q8_0" in tags
    assert "llama3.1:70b" not in tags  # filtered to the requested size


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


# ── per-model settings + model details (#model-settings) ─────────────────────


def test_model_settings_routes_present() -> None:
    paths = _app().openapi()["paths"]
    assert "/platform/v1/llm/model-settings" in paths
    assert "/platform/v1/llm/models/details" in paths


async def test_get_model_settings_defaults_to_inherit() -> None:
    ms = await _fresh_model_settings()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(model_settings=ms)), base_url="http://test"
    ) as client:
        resp = await client.get("/platform/v1/llm/model-settings", params={"model": "llama3.2"})
    assert resp.status_code == 200
    assert resp.json() == {"context_window": None, "keep_alive": None, "device": None}


async def test_put_then_get_model_settings_round_trips() -> None:
    ms = await _fresh_model_settings()
    app = _app(model_settings=ms)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        put = await client.put(
            "/platform/v1/llm/model-settings",
            json={"model": "llama3.2:latest", "context_window": 8192, "keep_alive": "30m"},
        )
        assert put.status_code == 200
        got = await client.get(
            "/platform/v1/llm/model-settings", params={"model": "llama3.2:latest"}
        )
    assert got.json() == {"context_window": 8192, "keep_alive": "30m", "device": None}


async def test_put_model_settings_without_store_is_503() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(model_settings=None)), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/platform/v1/llm/model-settings", json={"model": "llama3.2", "context_window": 4096}
        )
    assert resp.status_code == 503


async def test_model_details_route_returns_show() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/platform/v1/llm/models/details", params={"model": "llama3.2:latest"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["quantization"] == "Q4_K_M"
    assert body["context_length"] == 131072


async def test_kv_cache_type_route_present_and_round_trips() -> None:
    prefs = await _fresh_prefs()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(prefs=prefs)), base_url="http://test"
    ) as client:
        put = await client.put("/platform/v1/llm/prefs/kv-cache-type", json={"value": "q8_0"})
        assert put.status_code == 200
        assert put.json()["applied"] is False  # no runtime wired → manual-restart fallback
        got = await client.get("/platform/v1/llm/prefs")
    assert got.json()["kv_cache_type"] == "q8_0"


async def test_kv_cache_type_route_applies_when_runtime_present() -> None:
    prefs = await _fresh_prefs()

    class _FakeRuntime:
        def __init__(self) -> None:
            self.applied: list[str | None] = []

        def apply_kv_cache_type(self, value: str | None) -> bool:
            self.applied.append(value)
            return True

    runtime = _FakeRuntime()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=_app(prefs=prefs, ollama_runtime=runtime)  # type: ignore[arg-type]
        ),
        base_url="http://test",
    ) as client:
        put = await client.put("/platform/v1/llm/prefs/kv-cache-type", json={"value": "q4_0"})
    assert put.status_code == 200
    assert put.json()["applied"] is True
    assert runtime.applied == ["q4_0"]  # the choice was pushed to the live runtime


async def test_model_settings_device_round_trips() -> None:
    ms = await _fresh_model_settings()
    app = _app(model_settings=ms)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.put(
            "/platform/v1/llm/model-settings", json={"model": "llama3.2", "device": "cpu"}
        )
        got = await client.get("/platform/v1/llm/model-settings", params={"model": "llama3.2"})
    assert got.json()["device"] == "cpu"


# ── per-model context suggestion on download (#386) ──────────────────────────


def test_suggest_context_route_present() -> None:
    assert "/platform/v1/llm/model-settings/suggest-context" in _app().openapi()["paths"]


async def test_suggest_context_route_without_store_is_503() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(model_settings=None)), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/platform/v1/llm/model-settings/suggest-context", json={"model": "llama3.2:3b"}
        )
    assert resp.status_code == 503


async def test_suggest_context_route_persists_a_fresh_suggestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The heuristic itself is unit-tested in test_system_info; here we stub it to a fixed value
    # so the route's persist + response wiring is deterministic (no hardware dependence).
    import epicurus_core_app.llm.routes as routes_mod

    async def fake_suggest(_gw: object, _model: str, *, tenant_id: str | None = None) -> int:
        return 8192

    monkeypatch.setattr(routes_mod, "suggest_context_for_model", fake_suggest)
    ms = await _fresh_model_settings()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(model_settings=ms)), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/platform/v1/llm/model-settings/suggest-context", json={"model": "llama3.2:3b"}
        )
        assert resp.status_code == 200
        assert resp.json() == {"model": "llama3.2:3b", "context_window": 8192, "applied": True}
        # …and it's persisted as the model's per-model context.
        got = await client.get("/platform/v1/llm/model-settings", params={"model": "llama3.2:3b"})
    assert got.json()["context_window"] == 8192


async def test_suggest_context_route_does_not_clobber_an_existing_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import epicurus_core_app.llm.routes as routes_mod

    async def _boom(*_args: object, **_kwargs: object) -> int:
        raise AssertionError("suggest_context_for_model must not run when an override exists")

    monkeypatch.setattr(routes_mod, "suggest_context_for_model", _boom)
    ms = await _fresh_model_settings()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(model_settings=ms)), base_url="http://test"
    ) as client:
        # The operator already tuned this model — the suggestion must defer to their choice.
        await client.put(
            "/platform/v1/llm/model-settings",
            json={"model": "llama3.2:3b", "context_window": 4096},
        )
        resp = await client.post(
            "/platform/v1/llm/model-settings/suggest-context", json={"model": "llama3.2:3b"}
        )
        assert resp.json() == {"model": "llama3.2:3b", "context_window": 4096, "applied": False}
        got = await client.get("/platform/v1/llm/model-settings", params={"model": "llama3.2:3b"})
    assert got.json()["context_window"] == 4096


async def test_suggest_context_route_null_when_nothing_to_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import epicurus_core_app.llm.routes as routes_mod

    async def fake_suggest(_gw: object, _model: str, *, tenant_id: str | None = None) -> None:
        return None  # a hosted model / no local size → nothing to suggest

    monkeypatch.setattr(routes_mod, "suggest_context_for_model", fake_suggest)
    ms = await _fresh_model_settings()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(model_settings=ms)), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/platform/v1/llm/model-settings/suggest-context", json={"model": "claude/sonnet"}
        )
        assert resp.json() == {"model": "claude/sonnet", "context_window": None, "applied": False}
        got = await client.get("/platform/v1/llm/model-settings", params={"model": "claude/sonnet"})
    # Nothing was persisted — the model still inherits the global/env default.
    assert got.json()["context_window"] is None


# ── saved hosted models (#496) ────────────────────────────────────────────────


def test_saved_models_routes_present() -> None:
    assert "/platform/v1/llm/saved-models" in _app().openapi()["paths"]


async def test_saved_models_empty_without_store() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(saved_models=None)), base_url="http://test"
    ) as client:
        resp = await client.get("/platform/v1/llm/saved-models")
    assert resp.status_code == 200
    assert resp.json() == {"models": []}


async def test_saved_models_add_persists_with_provider() -> None:
    store = await _fresh_saved_models()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(saved_models=store)), base_url="http://test"
    ) as client:
        post = await client.post(
            "/platform/v1/llm/saved-models",
            json={"model": "claude/claude-3-5-sonnet-latest"},
        )
        assert post.status_code == 200
        get = await client.get("/platform/v1/llm/saved-models")
    assert get.json() == {
        "models": [{"model": "claude/claude-3-5-sonnet-latest", "provider": "claude"}]
    }


async def test_saved_models_rejects_local_id() -> None:
    """A local id (bare name or an unknown ``hf.co/…`` prefix) is not a hosted model — 400.

    This is the server-side half of the fix for the client's old ``includes("/")`` heuristic
    that let ``hf.co/…`` locals pollute the hosted list (#496)."""
    store = await _fresh_saved_models()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(saved_models=store)), base_url="http://test"
    ) as client:
        bare = await client.post("/platform/v1/llm/saved-models", json={"model": "llama3.2"})
        hf = await client.post(
            "/platform/v1/llm/saved-models", json={"model": "hf.co/org/model:tag"}
        )
        get = await client.get("/platform/v1/llm/saved-models")
    assert bare.status_code == 400
    assert hf.status_code == 400
    assert get.json() == {"models": []}  # neither landed


async def test_saved_models_rejects_provider_only_id() -> None:
    """A provider prefix with no model part ("claude/") names a hosted provider but not a hosted
    *model* — 400, and no junk ``claude/`` row is persisted (#537)."""
    store = await _fresh_saved_models()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(saved_models=store)), base_url="http://test"
    ) as client:
        resp = await client.post("/platform/v1/llm/saved-models", json={"model": "claude/"})
        get = await client.get("/platform/v1/llm/saved-models")
    assert resp.status_code == 400
    assert get.json() == {"models": []}  # nothing landed


async def test_saved_models_remove() -> None:
    store = await _fresh_saved_models()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(saved_models=store)), base_url="http://test"
    ) as client:
        await client.post("/platform/v1/llm/saved-models", json={"model": "gpt/gpt-4o"})
        delete = await client.delete(
            "/platform/v1/llm/saved-models", params={"model": "gpt/gpt-4o"}
        )
        assert delete.status_code == 200
        get = await client.get("/platform/v1/llm/saved-models")
    assert get.json() == {"models": []}


async def test_saved_models_mutations_without_store_are_503() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(saved_models=None)), base_url="http://test"
    ) as client:
        post = await client.post("/platform/v1/llm/saved-models", json={"model": "gpt/gpt-4o"})
        delete = await client.delete(
            "/platform/v1/llm/saved-models", params={"model": "gpt/gpt-4o"}
        )
    assert post.status_code == 503
    assert delete.status_code == 503
