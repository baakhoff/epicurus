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


def create_llm_router(gateway: LlmGateway) -> APIRouter:
    """Gateway management routes — model catalog, providers, pulls.

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
