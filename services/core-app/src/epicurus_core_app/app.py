"""The epicurus core runtime service — the brain the platform is built on.

Across Phase 1 this service grows to host the agent loop, the LLM gateway, memory,
the power-state machine, and the MCP host that drives modules' tools. It stands the
container up: the ops surface (``/health`` + ``/metrics``), a connected NATS event
bus, the module-facing **platform API**, and the **LLM gateway** + **power** control.

Unlike a sidecar module (which exposes MCP tools *to* the agent), the core is the
**host** — so it serves a platform API and drives modules, rather than mounting its
own MCP tool server (ADR-0004 / ADR-0009).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from epicurus_core import EventBus, SecretStore, add_ops_routes, configure_logging, get_logger
from epicurus_core_app.llm.gateway import LlmGateway
from epicurus_core_app.llm.power import GatewayPausedError, PowerController
from epicurus_core_app.llm.routes import create_llm_router, create_power_router
from epicurus_core_app.platform_api import create_platform_router
from epicurus_core_app.settings import CoreAppSettings

SERVICE_NAME = "core-app"


def _service_version() -> str:
    """The installed distribution version, for ``/health``."""
    try:
        return pkg_version("epicurus-core-app")
    except PackageNotFoundError:
        return "0.0.0"


def create_app() -> FastAPI:
    """Build the core runtime ASGI app (connects to NATS on startup)."""
    settings = CoreAppSettings(service_name=SERVICE_NAME)
    configure_logging(settings)
    log = get_logger(SERVICE_NAME)
    bus = EventBus.from_settings(settings)
    power = PowerController()
    gateway = LlmGateway(
        ollama_url=settings.ollama_url,
        default_model=settings.llm_default_model,
        keep_alive=settings.llm_keep_alive,
        power=power,
        secrets=SecretStore.from_settings(settings),
        default_tenant=settings.default_tenant_id,
        bus=bus,
        fallbacks=settings.fallback_models,
        num_retries=settings.llm_num_retries,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await bus.connect()
        log.info("core runtime ready", tenant=settings.default_tenant_id)
        try:
            yield
        finally:
            await bus.close()

    app = FastAPI(title="epicurus core", lifespan=lifespan)
    add_ops_routes(app, service_name=SERVICE_NAME, version=_service_version())
    app.include_router(create_platform_router(settings))
    app.include_router(create_llm_router(gateway))
    app.include_router(create_power_router(gateway, power))

    @app.exception_handler(GatewayPausedError)
    async def _on_paused(_request: Request, exc: GatewayPausedError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    return app


app = create_app()
