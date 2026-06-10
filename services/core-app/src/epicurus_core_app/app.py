"""The epicurus core runtime service — the brain the platform is built on.

Across Phase 1 this service grows to host the agent loop, the LLM gateway, memory,
the power-state machine, and the MCP host that drives modules' tools. This skeleton
stands the container up: the ops surface (``/health`` + ``/metrics``), a connected
NATS event bus, and the module-facing **platform API**. Capabilities plug in as the
later Phase-1 cards land.

Unlike a sidecar module (which exposes MCP tools *to* the agent), the core is the
**host** — so it serves a platform API and drives modules, rather than mounting its
own MCP tool server (ADR-0004 / ADR-0009).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version

from fastapi import FastAPI

from epicurus_core import CoreSettings, EventBus, add_ops_routes, configure_logging, get_logger
from epicurus_core_app.platform_api import create_platform_router

SERVICE_NAME = "core-app"


def _service_version() -> str:
    """The installed distribution version, for ``/health``."""
    try:
        return pkg_version("epicurus-core-app")
    except PackageNotFoundError:
        return "0.0.0"


def create_app() -> FastAPI:
    """Build the core runtime ASGI app (connects to NATS on startup)."""
    settings = CoreSettings(service_name=SERVICE_NAME)
    configure_logging(settings)
    log = get_logger(SERVICE_NAME)
    bus = EventBus.from_settings(settings)

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
    return app


app = create_app()
