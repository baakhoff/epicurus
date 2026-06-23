"""HTTP surface for the LLM gateway and power control, under /platform/v1."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from epicurus_core_app.llm.gateway import LlmGateway, UnknownProviderError
from epicurus_core_app.llm.models import ModelInfo, PowerState, ProviderInfo
from epicurus_core_app.llm.power import PowerController
from epicurus_core_app.llm.prefs import LlmPrefsStore

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    # Tell buffering proxies (the web container's nginx) to pass events through.
    "X-Accel-Buffering": "no",
}


class PullRequest(BaseModel):
    model: str


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


class SetHiddenRequest(BaseModel):
    """Body for PUT /llm/prefs/hidden — toggle one model's hidden state."""

    name: str
    hidden: bool


def create_llm_router(
    gateway: LlmGateway,
    prefs: LlmPrefsStore | None = None,
    default_tenant: str = "local",
) -> APIRouter:
    """Gateway management routes — model catalog, providers, pulls, and prefs.

    Chat completions go through the single module-facing path
    ``POST /platform/v1/chat`` (ADR-0021); the gateway no longer exposes its own
    ``/llm/chat``.
    """
    router = APIRouter(prefix="/platform/v1/llm", tags=["llm"])

    @router.get("/models", response_model=list[ModelInfo])
    async def list_models() -> list[ModelInfo]:
        return await gateway.models()

    @router.delete("/models")
    async def delete_model(name: str) -> dict[str, str]:
        """Remove a local model. ``name`` is a query param — model names contain
        ``:`` and ``/`` (e.g. ``hf.co/org/model:tag``), which proxies may mangle
        in a path."""
        await gateway.delete_model(name)
        return {"status": "ok", "model": name}

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
                hidden=[],
            )
        stored_default = await prefs.get_default(default_tenant)
        stored_embed_default = await prefs.get_embed_default(default_tenant)
        stored_context_window = await prefs.get_context_window(default_tenant)
        hidden = await prefs.get_hidden(default_tenant)
        return LlmPrefsResponse(
            global_default=stored_default,
            global_embed_default=stored_embed_default,
            global_context_window=stored_context_window,
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
