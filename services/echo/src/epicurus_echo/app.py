"""The runnable echo service: ops endpoints + the MCP tool surface over HTTP,
with the NATS responder wired on startup.

This is the reference shape for a module's container: a FastAPI app exposing
`/health` + `/metrics`, mounting the module's MCP server at `/mcp`, and running
its NATS responder for the lifetime of the process.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from typing import Any

from fastapi import FastAPI, HTTPException

from epicurus_core import (
    CoreSettings,
    EventBus,
    add_manifest_route,
    add_ops_routes,
    configure_logging,
    get_logger,
)
from epicurus_echo.service import ECHO_PAGE_ID, build_module, echo_page, serve_responder


def _service_version() -> str:
    """The installed echo distribution version, for ``/health``."""
    try:
        return pkg_version("epicurus-echo")
    except PackageNotFoundError:
        return "0.0.0"


def create_app() -> FastAPI:
    """Build the echo ASGI app (does not connect to anything until startup)."""
    settings = CoreSettings(service_name="echo")
    configure_logging(settings)
    log = get_logger("echo")
    module = build_module()
    bus = EventBus.from_settings(settings)
    mcp_app = module.http_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Run the MCP streamable-HTTP session manager alongside the NATS responder.
        async with module.mcp.session_manager.run():
            await bus.connect()
            await serve_responder(bus, settings.default_tenant_id)
            log.info("echo service ready", tenant=settings.default_tenant_id)
            try:
                yield
            finally:
                await bus.close()

    app = FastAPI(title="echo", lifespan=lifespan)
    add_ops_routes(app, service_name="echo", version=_service_version())
    add_manifest_route(app, module)

    @app.get("/pages/{page_id}")
    async def page(page_id: str) -> dict[str, Any]:
        """Serve a manifest-declared page's data (ADR-0018); the core proxies this."""
        if page_id != ECHO_PAGE_ID:
            raise HTTPException(status_code=404, detail=f"no page {page_id!r}")
        return echo_page()

    app.mount("/mcp", mcp_app)
    return app


app = create_app()
