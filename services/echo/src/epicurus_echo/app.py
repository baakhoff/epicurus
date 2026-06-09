"""The runnable echo service: ops endpoints + the MCP tool surface over HTTP,
with the NATS responder wired on startup.

This is the reference shape for a module's container: a FastAPI app exposing
`/health` + `/metrics`, mounting the module's MCP server at `/mcp`, and running
its NATS responder for the lifetime of the process.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from epicurus_core import CoreSettings, EventBus, add_ops_routes, configure_logging, get_logger
from epicurus_echo.service import build_module, serve_responder


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
            yield
            await bus.close()

    app = FastAPI(title="echo", lifespan=lifespan)
    add_ops_routes(app, service_name="echo")
    app.mount("/mcp", mcp_app)
    return app


app = create_app()
