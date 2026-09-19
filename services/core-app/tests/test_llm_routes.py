"""Tests for the LLM gateway router after the chat-surface cleanup (#114)."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core_app.llm.catalog import CatalogEntry, ModelCatalog
from epicurus_core_app.llm.errors import LocalRuntimeState, LocalRuntimeUnavailableError
from epicurus_core_app.llm.model_settings import ModelSettingsStore
from epicurus_core_app.llm.models import ModelDetails, ModelInfo
from epicurus_core_app.llm.ollama_runtime import KvCacheApplyResult, OllamaRuntime
from epicurus_core_app.llm.prefs import LlmPrefsStore
from epicurus_core_app.llm.routes import create_llm_router
from epicurus_core_app.llm.saved_models import SavedHostedModelStore
from epicurus_core_app.llm.variants import VariantLookup


class _StubGateway:
    """Only needs to exist — most tests inspect routes, not call behavior.

    ``show`` backs the /models/details route; ``model_role`` backs the role gate on the two
    default-setting routes (#944), answering ``unknown`` for anything not in ``roles`` — the
    "catalogue says nothing" case, which the gate lets through; ``unload`` records its calls so
    the unload route can be asserted. ``local_runtime_enabled`` / ``require_local_runtime`` /
    ``local_runtime_state`` back the three-state local-runtime contract (#962): build it with
    ``local_runtime=False`` for a hosted-only deployment, where every local-only write route
    must refuse with 409 instead of reaching a runtime that is not there.
    """

    def __init__(self, roles: dict[str, str] | None = None, *, local_runtime: bool = True) -> None:
        self.unloaded: list[str | None] = []
        self.roles = roles or {}
        self._local_runtime = local_runtime
        self.pulled: list[str] = []
        self.deleted: list[str] = []

    @property
    def local_runtime_enabled(self) -> bool:
        return self._local_runtime

    def require_local_runtime(self, action: str) -> None:
        if self._local_runtime:
            return
        raise LocalRuntimeUnavailableError(
            state="absent",
            message=f"cannot {action}: this deployment runs no local LLM runtime",
        )

    async def local_runtime_state(self) -> LocalRuntimeState:
        return "ok" if self._local_runtime else "absent"

    async def models(
        self, tenant_id: str | None = None, *, with_capabilities: bool = False
    ) -> list[ModelInfo]:
        # What the real gateway answers in both non-serving states (#962): an empty list.
        return [] if not self._local_runtime else [ModelInfo(name="llama3.2:latest")]

    async def pull(self, model: str) -> None:
        self.require_local_runtime("pull a model")
        self.pulled.append(model)

    async def delete_model(self, model: str) -> None:
        self.require_local_runtime("delete a model")
        self.deleted.append(model)

    async def show(self, model: str, tenant_id: str | None = None) -> ModelDetails:
        return ModelDetails(
            quantization="Q4_K_M", parameter_size="8.0B", context_length=131072, family="llama"
        )

    async def model_role(self, model: str | None = None, tenant_id: str | None = None) -> str:
        return self.roles.get(model or "", "unknown")

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


@pytest.mark.parametrize("steps", [1, 12, 40, 500])
async def test_agent_max_steps_route_stores_any_bound_unchanged(steps: int) -> None:
    """No ceiling since #925 — what the operator asks for is what is stored and read back.

    The 1-12 clamp used to rewrite a 40 to 12 *silently*, which is the whole defect: a long
    task ran out of rounds and nothing said why. 12 stays in the list so the old ceiling is
    pinned as an ordinary value, not a boundary.
    """
    prefs = await _fresh_prefs()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(prefs=prefs)), base_url="http://test"
    ) as client:
        put = await client.put("/platform/v1/llm/prefs/agent-max-steps", json={"value": steps})
        assert put.status_code == 200
        assert put.json()["value"] == steps
        got = await client.get("/platform/v1/llm/prefs")
    assert got.json()["global_agent_max_steps"] == steps


async def test_agent_max_steps_floor_and_clear() -> None:
    """The floor survives the lifted ceiling: 0 and negatives still mean "at least one round"."""
    prefs = await _fresh_prefs()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(prefs=prefs)), base_url="http://test"
    ) as client:
        floored = await client.put("/platform/v1/llm/prefs/agent-max-steps", json={"value": 0})
        assert floored.json()["value"] == 1  # clamped up to the floor
        negative = await client.put("/platform/v1/llm/prefs/agent-max-steps", json={"value": -5})
        assert negative.json()["value"] == 1
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


class _FakeRuntime:
    """Records the choices pushed at it and reports a canned outcome."""

    def __init__(self, result: KvCacheApplyResult | None = None) -> None:
        self.applied: list[str | None] = []
        self._result = result or KvCacheApplyResult(applied=True, staged=True)

    def apply_kv_cache_type(self, value: str | None) -> KvCacheApplyResult:
        self.applied.append(value)
        return self._result


async def _put_kv_cache_type(runtime: object | None, value: str | None = "q4_0") -> httpx.Response:
    prefs = await _fresh_prefs()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=_app(prefs=prefs, ollama_runtime=runtime)  # type: ignore[arg-type]
        ),
        base_url="http://test",
    ) as client:
        return await client.put("/platform/v1/llm/prefs/kv-cache-type", json={"value": value})


async def test_kv_cache_type_route_applies_when_runtime_present() -> None:
    runtime = _FakeRuntime()
    put = await _put_kv_cache_type(runtime)
    assert put.status_code == 200
    assert put.json()["applied"] is True
    assert put.json()["staged"] is True  # applied always implies staged
    assert runtime.applied == ["q4_0"]  # the choice was pushed to the live runtime


async def test_kv_cache_type_route_reports_staged_when_only_the_restart_is_missing() -> None:
    """The usual degraded install (#709): the env file holds the choice, Docker isn't wired.

    The UI branches on this to say "restart the container" instead of sending the operator to
    edit environment variables they never needed to touch.
    """
    put = await _put_kv_cache_type(_FakeRuntime(KvCacheApplyResult(applied=False, staged=True)))
    body = put.json()
    assert body["applied"] is False
    assert body["staged"] is True


async def test_kv_cache_type_route_reports_unstaged_when_the_write_failed() -> None:
    """The only case where the manual environment-variable route is the real one."""
    put = await _put_kv_cache_type(_FakeRuntime(KvCacheApplyResult(applied=False, staged=False)))
    body = put.json()
    assert body["applied"] is False
    assert body["staged"] is False


async def test_kv_cache_type_route_is_unstaged_without_a_runtime() -> None:
    # Nothing wrote the env file at all, so the manual route is all that's left.
    put = await _put_kv_cache_type(None)
    assert put.json() == {"status": "ok", "value": "q4_0", "applied": False, "staged": False}


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
    # context_length/capabilities (#618) come from gateway.show() per saved model —
    # _StubGateway.show() always returns the same fixed ModelDetails regardless of input.
    assert get.json() == {
        "models": [
            {
                "model": "claude/claude-3-5-sonnet-latest",
                "provider": "claude",
                "context_length": 131072,
                "capabilities": [],
                "role": "unknown",
                "in_catalogue": None,
                # No override set — the defaults say "trust the map" (#711).
                "override": {
                    "vision": "auto",
                    "tools": "auto",
                    "role": "auto",
                    "context_length": None,
                    "tools_learned": None,
                },
            }
        ]
    }


async def test_saved_models_enriches_each_from_its_own_show_call() -> None:
    """Each saved model gets *its own* details, not one blob reused for every row (#618)."""

    class _PerModelGateway(_StubGateway):
        async def show(self, model: str, tenant_id: str | None = None) -> ModelDetails:
            return {
                "claude/claude-3-7-sonnet-20250219": ModelDetails(
                    context_length=200000, capabilities=["tools", "vision"]
                ),
                "gpt/gpt-4o-mini": ModelDetails(context_length=None, capabilities=["tools"]),
            }[model]

    store = await _fresh_saved_models()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(gateway=_PerModelGateway(), saved_models=store)),
        base_url="http://test",
    ) as client:
        await client.post(
            "/platform/v1/llm/saved-models", json={"model": "claude/claude-3-7-sonnet-20250219"}
        )
        await client.post("/platform/v1/llm/saved-models", json={"model": "gpt/gpt-4o-mini"})
        get = await client.get("/platform/v1/llm/saved-models")
    by_model = {m["model"]: m for m in get.json()["models"]}
    assert by_model["claude/claude-3-7-sonnet-20250219"]["context_length"] == 200000
    assert by_model["claude/claude-3-7-sonnet-20250219"]["capabilities"] == ["tools", "vision"]
    assert by_model["gpt/gpt-4o-mini"]["context_length"] is None  # never a fake default
    assert by_model["gpt/gpt-4o-mini"]["capabilities"] == ["tools"]


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


# ── saved-model capability overrides (#711) ───────────────────────────────────


async def test_capability_override_round_trips_the_editor() -> None:
    """The editor's contract: what you PUT comes back on the next list, verbatim."""
    store = await _fresh_saved_models()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(saved_models=store)), base_url="http://test"
    ) as client:
        await client.post("/platform/v1/llm/saved-models", json={"model": "grok/grok-latest"})
        put = await client.put(
            "/platform/v1/llm/saved-models/capabilities",
            json={
                "model": "grok/grok-latest",
                "vision": "on",
                "tools": "off",
                "role": "chat",
                "context_length": 256000,
            },
        )
        assert put.status_code == 200
        get = await client.get("/platform/v1/llm/saved-models")
    row = next(m for m in get.json()["models"] if m["model"] == "grok/grok-latest")
    assert row["override"] == {
        "vision": "on",
        "tools": "off",
        "role": "chat",
        "context_length": 256000,
        "tools_learned": None,
    }


async def test_capability_override_auto_clears_back_to_the_map() -> None:
    store = await _fresh_saved_models()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(saved_models=store)), base_url="http://test"
    ) as client:
        await client.post("/platform/v1/llm/saved-models", json={"model": "grok/grok-latest"})
        await client.put(
            "/platform/v1/llm/saved-models/capabilities",
            json={"model": "grok/grok-latest", "vision": "on", "context_length": 256000},
        )
        cleared = await client.put(
            "/platform/v1/llm/saved-models/capabilities",
            json={"model": "grok/grok-latest", "vision": "auto", "context_length": None},
        )
        assert cleared.status_code == 200
        get = await client.get("/platform/v1/llm/saved-models")
    row = next(m for m in get.json()["models"] if m["model"] == "grok/grok-latest")
    assert row["override"] == {
        "vision": "auto",
        "tools": "auto",
        "role": "auto",
        "context_length": None,
        "tools_learned": None,
    }


async def test_capability_override_404s_for_an_unsaved_model() -> None:
    """An override is a property of a saved row — never a back door to creating one."""
    store = await _fresh_saved_models()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(saved_models=store)), base_url="http://test"
    ) as client:
        put = await client.put(
            "/platform/v1/llm/saved-models/capabilities",
            json={"model": "gpt/never-saved", "vision": "on"},
        )
        get = await client.get("/platform/v1/llm/saved-models")
    assert put.status_code == 404
    assert get.json() == {"models": []}


async def test_capability_override_rejects_a_bad_vision_value() -> None:
    store = await _fresh_saved_models()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(saved_models=store)), base_url="http://test"
    ) as client:
        await client.post("/platform/v1/llm/saved-models", json={"model": "grok/grok-latest"})
        bad_vision = await client.put(
            "/platform/v1/llm/saved-models/capabilities",
            json={"model": "grok/grok-latest", "vision": "maybe"},
        )
        bad_context = await client.put(
            "/platform/v1/llm/saved-models/capabilities",
            json={"model": "grok/grok-latest", "vision": "auto", "context_length": 0},
        )
    assert bad_vision.status_code == 422
    assert bad_context.status_code == 422  # a window of zero tokens is not a window


async def test_capability_override_503s_without_a_store() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(saved_models=None)), base_url="http://test"
    ) as client:
        put = await client.put(
            "/platform/v1/llm/saved-models/capabilities",
            json={"model": "grok/grok-latest", "vision": "on"},
        )
    assert put.status_code == 503


# ── The role gate on the two default-setting routes (#944) ────────────────────


async def test_set_default_rejects_an_embedding_model() -> None:
    """The write that created #944's incident: any string at all became the chat default."""
    prefs = await _fresh_prefs()
    gateway = _StubGateway({"openrouter/qwen/qwen3-embedding-8b": "embedding"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(prefs=prefs, gateway=gateway)),
        base_url="http://test",
    ) as client:
        resp = await client.put(
            "/platform/v1/llm/prefs/default",
            json={"model": "openrouter/qwen/qwen3-embedding-8b"},
        )
        current = await client.get("/platform/v1/llm/prefs")
    assert resp.status_code == 400
    assert "embedding model" in resp.json()["detail"]
    assert current.json()["global_default"] is None  # nothing was persisted


async def test_set_embed_default_rejects_a_chat_model() -> None:
    prefs = await _fresh_prefs()
    gateway = _StubGateway({"claude/claude-sonnet-4-6": "chat"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(prefs=prefs, gateway=gateway)),
        base_url="http://test",
    ) as client:
        resp = await client.put(
            "/platform/v1/llm/prefs/embed-default", json={"model": "claude/claude-sonnet-4-6"}
        )
    assert resp.status_code == 400
    assert "chat model" in resp.json()["detail"]


async def test_the_role_gate_lets_an_unknown_model_through() -> None:
    """A thin catalogue must not make a working model unselectable."""
    prefs = await _fresh_prefs()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(prefs=prefs, gateway=_StubGateway())),
        base_url="http://test",
    ) as client:
        resp = await client.put(
            "/platform/v1/llm/prefs/default", json={"model": "custom/never-listed"}
        )
        current = await client.get("/platform/v1/llm/prefs")
    assert resp.status_code == 200
    assert current.json()["global_default"] == "custom/never-listed"


async def test_clearing_a_default_is_never_role_checked() -> None:
    prefs = await _fresh_prefs()
    gateway = _StubGateway({"claude/claude-sonnet-4-6": "chat"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(prefs=prefs, gateway=gateway)),
        base_url="http://test",
    ) as client:
        assert (
            await client.put("/platform/v1/llm/prefs/embed-default", json={"model": None})
        ).status_code == 200


async def test_an_embedding_model_can_still_be_saved() -> None:
    """Deviation from #944's fix list: one saved list serves both roles since #865."""
    store = await _fresh_saved_models()
    gateway = _StubGateway({"openrouter/qwen/qwen3-embedding-8b": "embedding"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(saved_models=store, gateway=gateway)),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            "/platform/v1/llm/saved-models",
            json={"model": "openrouter/qwen/qwen3-embedding-8b"},
        )
    assert resp.status_code == 200
    assert await store.list("local") == ["openrouter/qwen/qwen3-embedding-8b"]


async def test_the_tools_override_round_trips_and_clears_the_learned_answer() -> None:
    store = await _fresh_saved_models()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(saved_models=store)), base_url="http://test"
    ) as client:
        await client.post("/platform/v1/llm/saved-models", json={"model": "grok/grok-latest"})
        await store.learn_tools_unsupported("local", "grok/grok-latest")
        listed = await client.get("/platform/v1/llm/saved-models")
        learned = next(m for m in listed.json()["models"] if m["model"] == "grok/grok-latest")
        assert learned["override"]["tools_learned"] == "off"
        assert learned["override"]["tools"] == "auto"  # distinguishable from an explicit off

        put = await client.put(
            "/platform/v1/llm/saved-models/capabilities",
            json={"model": "grok/grok-latest", "tools": "auto"},
        )
        assert put.status_code == 200
        again = await client.get("/platform/v1/llm/saved-models")
    row = next(m for m in again.json()["models"] if m["model"] == "grok/grok-latest")
    # Returning the control to Auto genuinely starts over (ADR-0140).
    assert row["override"]["tools_learned"] is None


async def test_capability_override_rejects_a_bad_tools_value() -> None:
    store = await _fresh_saved_models()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(saved_models=store)), base_url="http://test"
    ) as client:
        await client.post("/platform/v1/llm/saved-models", json={"model": "grok/grok-latest"})
        put = await client.put(
            "/platform/v1/llm/saved-models/capabilities",
            json={"model": "grok/grok-latest", "tools": "maybe"},
        )
    assert put.status_code == 422


async def test_capability_override_rejects_a_bad_role_value() -> None:
    store = await _fresh_saved_models()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(saved_models=store)), base_url="http://test"
    ) as client:
        await client.post("/platform/v1/llm/saved-models", json={"model": "grok/grok-latest"})
        put = await client.put(
            "/platform/v1/llm/saved-models/capabilities",
            json={"model": "grok/grok-latest", "role": "reranker"},
        )
    assert put.status_code == 422


# ── no local runtime at all (#962, ADR-0144) ─────────────────────────────────────
#
# `absent` answers 409 — the request is meaningless on this deployment and retrying will
# never help — and every one of these paths used to answer 500 or (worse) 200.


def _hosted_only_app() -> FastAPI:
    """An app whose gateway reports a hosted-only deployment (``OLLAMA_URL`` blank)."""
    return _app(gateway=_StubGateway(local_runtime=False), prefs=None)


async def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_the_local_runtime_endpoint_is_declared() -> None:
    assert "/platform/v1/llm/local-runtime" in _app().openapi()["paths"]


async def test_local_runtime_reports_ok_when_one_answers() -> None:
    async with await _client(_app()) as client:
        body = (await client.get("/platform/v1/llm/local-runtime")).json()
    assert body == {"state": "ok", "url_configured": True}


async def test_local_runtime_reports_absent_on_a_hosted_only_deployment() -> None:
    async with await _client(_hosted_only_app()) as client:
        response = await client.get("/platform/v1/llm/local-runtime")
    assert response.status_code == 200
    assert response.json() == {"state": "absent", "url_configured": False}


async def test_models_is_a_bare_empty_list_and_200_without_a_runtime() -> None:
    """The regression test for the 500 the Models page collected every ten seconds.

    The *shape* matters as much as the status: five web consumers and seven internal callers
    read this array, so the state went to its own endpoint rather than into an envelope here.
    """
    async with await _client(_hosted_only_app()) as client:
        response = await client.get("/platform/v1/llm/models")
    assert response.status_code == 200
    assert response.json() == []


async def test_pull_refuses_with_409_without_a_runtime() -> None:
    gateway = _StubGateway(local_runtime=False)
    async with await _client(_app(gateway=gateway)) as client:
        response = await client.post("/platform/v1/llm/pull", json={"model": "llama3.2"})
    assert response.status_code == 409
    assert "no local LLM runtime" in response.json()["detail"]
    assert gateway.pulled == []


async def test_pull_stream_refuses_before_the_stream_starts() -> None:
    """A 409, not a 200 whose only SSE event is an error — an SSE cannot take back its status."""
    async with await _client(_hosted_only_app()) as client:
        response = await client.post("/platform/v1/llm/pull/stream", json={"model": "llama3.2"})
    assert response.status_code == 409
    assert "text/event-stream" not in response.headers.get("content-type", "")


async def test_delete_refuses_with_409_without_a_runtime() -> None:
    gateway = _StubGateway(local_runtime=False)
    async with await _client(_app(gateway=gateway)) as client:
        response = await client.delete("/platform/v1/llm/models?name=llama3.2")
    assert response.status_code == 409
    assert gateway.deleted == []


async def test_unload_refuses_with_409_without_a_runtime() -> None:
    """The gateway's own unload stays silent (the power-pause path needs it); the route says so."""
    gateway = _StubGateway(local_runtime=False)
    async with await _client(_app(gateway=gateway)) as client:
        response = await client.post("/platform/v1/llm/unload", json={"model": None})
    assert response.status_code == 409
    assert gateway.unloaded == []


async def test_a_runtime_that_answers_with_an_error_is_a_502_not_a_500() -> None:
    """`unreachable` is an error — and the core is a gateway in front of it."""

    class _Unreachable(_StubGateway):
        async def pull(self, model: str) -> None:
            raise httpx.ConnectError("connection refused")

    async with await _client(_app(gateway=_Unreachable())) as client:
        response = await client.post("/platform/v1/llm/pull", json={"model": "llama3.2"})
    assert response.status_code == 502
    assert "unreachable" in response.json()["detail"]


async def test_kv_cache_type_refuses_with_409_and_persists_nothing_without_a_runtime() -> None:
    """The setting describes how Ollama *starts*; with no Ollama there is nothing to record."""
    prefs = await _fresh_prefs()
    runtime = _FakeRuntime()
    app = _app(
        prefs=prefs,
        ollama_runtime=runtime,  # type: ignore[arg-type]
        gateway=_StubGateway(local_runtime=False),
    )
    async with await _client(app) as client:
        response = await client.put("/platform/v1/llm/prefs/kv-cache-type", json={"value": "q4_0"})
    assert response.status_code == 409
    assert runtime.applied == []
    assert await prefs.get_kv_cache_type("local") is None
