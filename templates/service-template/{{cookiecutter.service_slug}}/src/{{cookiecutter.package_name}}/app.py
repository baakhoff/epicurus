"""Runnable {{ cookiecutter.service_name }} service: ops endpoints + the MCP tool
surface over HTTP, with a connected event bus for the lifetime of the process."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version

from fastapi import FastAPI

from epicurus_core import (
    CoreSettings,
    EventBus,
    add_manifest_route,
    add_ops_routes,
    configure_logging,
    get_logger,
    setup_tracing,
)
from {{ cookiecutter.package_name }}.service import MODULE_NAME, build_module


def _service_version() -> str:
    """The installed distribution version, for ``/health``."""
    try:
        return pkg_version("epicurus-{{ cookiecutter.service_slug }}")
    except PackageNotFoundError:
        return "0.0.0"


def create_app() -> FastAPI:
    settings = CoreSettings(service_name=MODULE_NAME)
    configure_logging(settings)
    log = get_logger(MODULE_NAME)
    module = build_module()
    bus = EventBus.from_settings(settings)
    mcp_app = module.http_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with module.mcp.session_manager.run():
            await bus.connect()
            # Register NATS subscribers/responders here, e.g.:
            #   await bus.reply("{{ cookiecutter.service_slug }}.request", handler,
            #                   tenant_id=settings.default_tenant_id)
            log.info("{{ cookiecutter.service_slug }} service ready")
            try:
                yield
            finally:
                await bus.close()

    app = FastAPI(title=MODULE_NAME, lifespan=lifespan)
    add_ops_routes(app, service_name=MODULE_NAME, version=_service_version())
    # Distributed tracing to Tempo (#57) — a no-op unless OTEL_TRACES_ENABLED is set.
    setup_tracing(app, settings, version=_service_version())
    # GET /manifest — how the core discovers this module's tools (ADR-0004). Without
    # it the agent never sees the module and the smoke gate's discovery check fails.
    add_manifest_route(app, module)
    app.mount("/mcp", mcp_app)
    return app


app = create_app()
