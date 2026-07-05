"""HTTP surface for the LLM gateway and power control, under /platform/v1."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from epicurus_core_app.llm.catalog import CatalogResponse, ModelCatalog
from epicurus_core_app.llm.gateway import LlmGateway, UnknownProviderError
from epicurus_core_app.llm.model_settings import ModelSettings, ModelSettingsStore
from epicurus_core_app.llm.models import ModelDetails, ModelInfo, PowerState, ProviderInfo
from epicurus_core_app.llm.ollama_runtime import OllamaRuntime
from epicurus_core_app.llm.power import PowerController
from epicurus_core_app.llm.prefs import LlmPrefsStore
from epicurus_core_app.llm.providers import is_hosted
from epicurus_core_app.llm.saved_models import SavedHostedModelStore
from epicurus_core_app.llm.variants import ModelVariantsResponse, VariantLookup
from epicurus_core_app.system_info import suggest_context_for_model

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    # Tell buffering proxies (the web container's nginx) to pass events through.
    "X-Accel-Buffering": "no",
}


class PullRequest(BaseModel):
    model: str


class UnloadRequest(BaseModel):
    # None = unload every loaded model; a name = just that one. Frees VRAM/RAM now without
    # touching power state (#331).
    model: str | None = None


class ProviderKeyRequest(BaseModel):
    api_key: str
    # OpenAI-compatible endpoint URL — required by the "custom" provider only.
    api_base: str | None = None


class PowerRequest(BaseModel):
    state: PowerState


class PowerStatus(BaseModel):
    state: PowerState


class LlmPrefsResponse(BaseModel):
    """Current persisted LLM preferences for the tenant."""

    global_default: str | None
    global_embed_default: str | None
    # Operator-chosen Ollama context window (num_ctx); NULL means the env/runtime default.
    global_context_window: int | None
    # Operator-chosen Ollama KV-cache type ("f16"|"q8_0"|"q4_0"); NULL = runtime default.
    kv_cache_type: str | None
    # Operator-chosen agent loop bound (tool rounds per turn); NULL = the env default.
    global_agent_max_steps: int | None
    hidden: list[str]


class SetDefaultRequest(BaseModel):
    """Body for PUT /llm/prefs/default."""

    model: str | None


class SetEmbedDefaultRequest(BaseModel):
    """Body for PUT /llm/prefs/embed-default."""

    model: str | None


class SetContextWindowRequest(BaseModel):
    """Body for PUT /llm/prefs/context-window."""

    value: int | None


class SetAgentMaxStepsRequest(BaseModel):
    """Body for PUT /llm/prefs/agent-max-steps."""

    value: int | None


class SetHiddenRequest(BaseModel):
    """Body for PUT /llm/prefs/hidden — toggle one model's hidden state."""

    name: str
    hidden: bool


class SetKvCacheTypeRequest(BaseModel):
    """Body for PUT /llm/prefs/kv-cache-type."""

    value: str | None


class SetModelSettingsRequest(BaseModel):
    """Body for PUT /llm/model-settings — one model's per-model tuning."""

    model: str
    context_window: int | None = None
    keep_alive: str | None = None
    # "gpu" | "cpu" | null (auto). Mapped to Ollama num_gpu; local models only.
    device: str | None = None


class SuggestModelContextRequest(BaseModel):
    """Body for POST /llm/model-settings/suggest-context — the model that was just pulled."""

    model: str


class SavedModel(BaseModel):
    """One saved hosted-model id plus its provider alias (the id's ``<provider>/`` prefix)."""

    model: str
    provider: str


class SavedModelsResponse(BaseModel):
    """The tenant's saved hosted-model ids, most-recently-saved first (#496)."""

    models: list[SavedModel]


class SaveModelRequest(BaseModel):
    """Body for POST /llm/saved-models — a hosted model id to persist for the tenant."""

    model: str


def create_llm_router(
    gateway: LlmGateway,
    prefs: LlmPrefsStore | None = None,
    default_tenant: str = "local",
    catalog: ModelCatalog | None = None,
    variants: VariantLookup | None = None,
    model_settings: ModelSettingsStore | None = None,
    ollama_runtime: OllamaRuntime | None = None,
    saved_models: SavedHostedModelStore | None = None,
) -> APIRouter:
    """Gateway management routes — installed models, the browse catalog, providers,
    pulls, and prefs.

    Chat completions go through the single module-facing path
    ``POST /platform/v1/chat`` (ADR-0021); the gateway no longer exposes its own
    ``/llm/chat``.
    """
    router = APIRouter(prefix="/platform/v1/llm", tags=["llm"])

    @router.get("/models", response_model=list[ModelInfo])
    async def list_models(capabilities: bool = False) -> list[ModelInfo]:
        """List local models. ``?capabilities=true`` additionally fills each model's reported
        capabilities (tools/vision/…) from ``/api/show`` — opt-in, one call per model, so the
        Models page can badge them while the chat picker stays light."""
        return await gateway.models(with_capabilities=capabilities)

    @router.get("/catalog", response_model=CatalogResponse)
    async def get_catalog() -> CatalogResponse:
        """The browsable model catalog the core parses from upstream (#269).

        Returns the cached snapshot (entries + provenance); ``stale`` flags a seed /
        last-good list served after a failed or skipped refresh. An empty list with
        ``stale=True`` means the catalog isn't wired (it should always be in the app).
        """
        if catalog is None:
            return CatalogResponse(entries=[], source="", updated_at=None, stale=True)
        return await catalog.snapshot()

    @router.get("/catalog/variants", response_model=ModelVariantsResponse)
    async def get_variants(model: str) -> ModelVariantsResponse:
        """The quant variants available for a model (#330), looked up on demand from the
        registry. ``model`` is a query param (names carry ``:``). Best-effort: an empty list
        means none were found / the lookup is unwired, and the UI falls back to the manual box.
        """
        if variants is None:
            return ModelVariantsResponse(model=model, variants=[])
        return await variants.variants(model)

    @router.delete("/models")
    async def delete_model(name: str) -> dict[str, str]:
        """Remove a local model. ``name`` is a query param — model names contain
        ``:`` and ``/`` (e.g. ``hf.co/org/model:tag``), which proxies may mangle
        in a path."""
        await gateway.delete_model(name)
        return {"status": "ok", "model": name}

    @router.get("/models/details", response_model=ModelDetails)
    async def model_details(model: str) -> ModelDetails:
        """Read-only facts about a local model (quantization, parameter size, trained
        context length) from the runtime's ``/api/show``, for the model-settings sheet.
        ``model`` is a query param for the same name-mangling reason as ``delete``."""
        return await gateway.show(model)

    @router.post("/unload")
    async def unload_models(request: UnloadRequest) -> dict[str, str]:
        """Drop model(s) from memory now (``keep_alive=0``) **without** changing power state
        (#331) — the standalone unload the Models page calls. ``model`` omitted unloads every
        loaded model; the ``loaded`` badge refreshes on success / the next poll."""
        await gateway.unload(request.model)
        return {"status": "ok", "model": request.model or "all"}

    @router.get("/providers", response_model=list[ProviderInfo])
    async def list_providers() -> list[ProviderInfo]:
        return await gateway.providers()

    @router.put("/providers/{alias}/key")
    async def set_provider_key(alias: str, request: ProviderKeyRequest) -> dict[str, str]:
        """Store a hosted provider's API key (core → OpenBao; never logged/returned)."""
        try:
            await gateway.set_provider_key(
                alias, api_key=request.api_key, api_base=request.api_base
            )
        except UnknownProviderError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "ok", "alias": alias}

    @router.delete("/providers/{alias}/key")
    async def clear_provider_key(alias: str) -> dict[str, str]:
        try:
            await gateway.clear_provider_key(alias)
        except UnknownProviderError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"status": "ok", "alias": alias}

    @router.post("/pull")
    async def pull(request: PullRequest) -> dict[str, str]:
        await gateway.pull(request.model)
        return {"status": "ok", "model": request.model}

    @router.post("/pull/stream")
    async def pull_stream(request: PullRequest) -> StreamingResponse:
        """Pull a model, streaming the runtime's progress as SSE."""

        async def events() -> AsyncIterator[str]:
            try:
                async for item in gateway.pull_stream(request.model):
                    yield f"event: progress\ndata: {json.dumps(item)}\n\n"
                yield 'event: done\ndata: {"status": "ok"}\n\n'
            except Exception as exc:  # the response already started — finish with an error
                yield f"event: error\ndata: {json.dumps({'detail': str(exc)})}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream", headers=SSE_HEADERS)

    # ── LLM preferences (hide + global default) ──────────────────────────────

    @router.get("/prefs", response_model=LlmPrefsResponse)
    async def get_prefs() -> LlmPrefsResponse:
        """Return the tenant's stored global defaults and hidden-model list."""
        if prefs is None:
            return LlmPrefsResponse(
                global_default=None,
                global_embed_default=None,
                global_context_window=None,
                kv_cache_type=None,
                global_agent_max_steps=None,
                hidden=[],
            )
        stored_default = await prefs.get_default(default_tenant)
        stored_embed_default = await prefs.get_embed_default(default_tenant)
        stored_context_window = await prefs.get_context_window(default_tenant)
        stored_kv_cache_type = await prefs.get_kv_cache_type(default_tenant)
        stored_agent_max_steps = await prefs.get_agent_max_steps(default_tenant)
        hidden = await prefs.get_hidden(default_tenant)
        return LlmPrefsResponse(
            global_default=stored_default,
            global_embed_default=stored_embed_default,
            global_context_window=stored_context_window,
            kv_cache_type=stored_kv_cache_type,
            global_agent_max_steps=stored_agent_max_steps,
            hidden=hidden,
        )

    @router.put("/prefs/default")
    async def set_default(request: SetDefaultRequest) -> dict[str, str | None]:
        """Set or clear the global default chat model for this tenant."""
        if prefs is None:
            raise HTTPException(status_code=503, detail="preferences store not available")
        await prefs.set_default(default_tenant, request.model)
        return {"status": "ok", "model": request.model}

    @router.put("/prefs/embed-default")
    async def set_embed_default(request: SetEmbedDefaultRequest) -> dict[str, str | None]:
        """Set or clear the global default embedding model for this tenant."""
        if prefs is None:
            raise HTTPException(status_code=503, detail="preferences store not available")
        await prefs.set_embed_default(default_tenant, request.model)
        return {"status": "ok", "model": request.model}

    @router.put("/prefs/context-window")
    async def set_context_window(request: SetContextWindowRequest) -> dict[str, int | None | str]:
        """Set or clear the Ollama context window (num_ctx) for this tenant."""
        if prefs is None:
            raise HTTPException(status_code=503, detail="preferences store not available")
        await prefs.set_context_window(default_tenant, request.value)
        return {"status": "ok", "value": request.value}

    @router.put("/prefs/kv-cache-type")
    async def set_kv_cache_type(request: SetKvCacheTypeRequest) -> dict[str, str | bool | None]:
        """Set the operator's Ollama KV-cache type and apply it to the live runtime.

        Persists the choice, then — when Docker is wired — writes Ollama's start-up env file and
        restarts the container so it takes effect; flash attention is enabled automatically for
        the quantized types (#307, amends ADR-0046). ``applied`` is ``False`` when Docker is
        unavailable, in which case the UI falls back to the manual-restart instructions.
        """
        if prefs is None:
            raise HTTPException(status_code=503, detail="preferences store not available")
        await prefs.set_kv_cache_type(default_tenant, request.value)
        applied = ollama_runtime.apply_kv_cache_type(request.value) if ollama_runtime else False
        return {"status": "ok", "value": request.value, "applied": applied}

    @router.put("/prefs/agent-max-steps")
    async def set_agent_max_steps(request: SetAgentMaxStepsRequest) -> dict[str, int | None | str]:
        """Set or clear the agent loop bound (tool rounds per turn) for this tenant.

        Clamped to 1-12: at least one round to be useful, and a ceiling so a misconfigured
        value can't let a turn run away. ``null`` clears the override (back to the env default).
        """
        if prefs is None:
            raise HTTPException(status_code=503, detail="preferences store not available")
        value = None if request.value is None else max(1, min(12, request.value))
        await prefs.set_agent_max_steps(default_tenant, value)
        return {"status": "ok", "value": value}

    @router.put("/prefs/hidden")
    async def set_hidden(request: SetHiddenRequest) -> dict[str, object]:
        """Toggle one model's hidden state; returns the updated hidden list."""
        if prefs is None:
            raise HTTPException(status_code=503, detail="preferences store not available")
        current = await prefs.get_hidden(default_tenant)
        updated: list[str]
        if request.hidden and request.name not in current:
            updated = [*current, request.name]
        elif not request.hidden:
            updated = [m for m in current if m != request.name]
        else:
            updated = current
        await prefs.set_hidden(default_tenant, updated)
        return {"status": "ok", "hidden": updated}

    # ── Saved hosted models (first-class hosted ids, #496) ────────────────────

    @router.get("/saved-models", response_model=SavedModelsResponse)
    async def list_saved_models() -> SavedModelsResponse:
        """The tenant's saved hosted-model ids, most-recently-saved first.

        This is the source of truth the chat picker renders and the Models page lists — the
        browser's ``recentModels`` cache is only a warm fallback. Each entry carries the
        provider alias (the id's ``<provider>/`` prefix) so the UI can group by provider.
        """
        if saved_models is None:
            return SavedModelsResponse(models=[])
        ids = await saved_models.list(default_tenant)
        return SavedModelsResponse(
            models=[SavedModel(model=m, provider=m.split("/", 1)[0]) for m in ids]
        )

    @router.post("/saved-models")
    async def add_saved_model(request: SaveModelRequest) -> dict[str, str]:
        """Persist a hosted model id for the tenant (idempotent; a re-save bumps it first).

        Rejects anything that isn't a hosted id (a known ``<provider>/`` prefix) with 400, so a
        local ``hf.co/org/model:tag`` can never land here — the server-side half of the fix for
        the client's old ``includes("/")`` misclassification (#496).
        """
        if saved_models is None:
            raise HTTPException(status_code=503, detail="saved-models store not available")
        model = request.model.strip()
        if not is_hosted(model):
            raise HTTPException(
                status_code=400,
                detail=f"{model!r} is not a hosted model id (expected <provider>/<model>)",
            )
        await saved_models.add(default_tenant, model)
        return {"status": "ok", "model": model}

    @router.delete("/saved-models")
    async def remove_saved_model(model: str) -> dict[str, str]:
        """Forget a saved hosted model. ``model`` is a query param — ids carry ``/`` and ``:``
        which proxies may mangle in a path (mirrors ``DELETE /models``)."""
        if saved_models is None:
            raise HTTPException(status_code=503, detail="saved-models store not available")
        await saved_models.remove(default_tenant, model)
        return {"status": "ok", "model": model}

    # ── Per-model settings (context window + keep-alive) ──────────────────────

    @router.get("/model-settings", response_model=ModelSettings)
    async def get_model_settings(model: str) -> ModelSettings:
        """One model's stored settings (all-``None`` = inherit). ``model`` is a query
        param — names contain ``:``/``/`` which proxies may mangle in a path."""
        if model_settings is None:
            return ModelSettings()
        return await model_settings.get(default_tenant, model)

    @router.put("/model-settings")
    async def set_model_settings(request: SetModelSettingsRequest) -> dict[str, object]:
        """Set or clear one model's context window, keep-alive, and device (an all-``None``
        body removes the override, returning the model to the inherited defaults)."""
        if model_settings is None:
            raise HTTPException(status_code=503, detail="model-settings store not available")
        await model_settings.set(
            default_tenant,
            request.model,
            ModelSettings(
                context_window=request.context_window,
                keep_alive=request.keep_alive,
                device=request.device,
            ),
        )
        return {"status": "ok", "model": request.model}

    @router.post("/model-settings/suggest-context")
    async def suggest_model_context(
        request: SuggestModelContextRequest,
    ) -> dict[str, object]:
        """Compute and persist a recommended per-model context window for a freshly pulled model
        (#386), so it opens with a window sized to itself instead of the global default. The core
        owns the heuristic (VRAM + model size + KV cache); the web calls this when a pull finishes.

        Non-destructive: an existing per-model context override is left untouched. Returns the
        resolved per-model context and whether this call applied a new suggestion — ``applied`` is
        false when one was already set, or none could be computed (e.g. a hosted model with no
        local size)."""
        if model_settings is None:
            raise HTTPException(status_code=503, detail="model-settings store not available")
        existing = await model_settings.get(default_tenant, request.model)
        if existing.context_window is not None:
            return {
                "model": request.model,
                "context_window": existing.context_window,
                "applied": False,
            }
        suggested = await suggest_context_for_model(
            gateway, request.model, tenant_id=default_tenant
        )
        if suggested is None:
            return {"model": request.model, "context_window": None, "applied": False}
        await model_settings.set(
            default_tenant,
            request.model,
            existing.model_copy(update={"context_window": suggested}),
        )
        return {"model": request.model, "context_window": suggested, "applied": True}

    return router


def create_power_router(gateway: LlmGateway, power: PowerController) -> APIRouter:
    """Routes for the main-page power toggle (ADR-0005)."""
    router = APIRouter(prefix="/platform/v1", tags=["power"])

    @router.get("/power", response_model=PowerStatus)
    def get_power() -> PowerStatus:
        return PowerStatus(state=power.state)

    @router.put("/power", response_model=PowerStatus)
    async def set_power(request: PowerRequest) -> PowerStatus:
        if request.state is PowerState.PAUSED:
            power.pause()
            await gateway.unload()
        else:
            power.resume()
        return PowerStatus(state=power.state)

    return router
