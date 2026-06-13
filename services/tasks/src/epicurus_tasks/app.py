"""Runnable tasks service: ops endpoints + the MCP tool surface over HTTP."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from typing import Any

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_core import (
    EventBus,
    PlatformClient,
    add_manifest_route,
    add_ops_routes,
    configure_logging,
    get_logger,
)
from epicurus_tasks.db import TaskStore
from epicurus_tasks.google_provider import GoogleTasksProvider
from epicurus_tasks.local_provider import LocalTasksProvider
from epicurus_tasks.providers import TasksProvider
from epicurus_tasks.service import MODULE_NAME, build_module
from epicurus_tasks.settings import TasksSettings


def _service_version() -> str:
    try:
        return pkg_version("epicurus-tasks")
    except PackageNotFoundError:
        return "0.0.0"


def create_app() -> FastAPI:
    """Build the tasks ASGI app — wires the configured provider at startup."""
    settings = TasksSettings(service_name=MODULE_NAME)
    configure_logging(settings)
    log = get_logger(MODULE_NAME)

    # Resolve the active provider from settings.
    provider: TasksProvider
    store: TaskStore | None = None
    engine = None

    if settings.tasks_provider == "google":
        platform = PlatformClient(
            base_url=settings.platform_url, tenant_id=settings.default_tenant_id
        )
        provider = GoogleTasksProvider(platform=platform)
        log.info("tasks provider: google")
    else:
        engine = create_async_engine(settings.database_url)
        store = TaskStore(engine)
        provider = LocalTasksProvider(store)
        log.info("tasks provider: local")

    bus = EventBus.from_settings(settings)
    module = build_module(provider, tenant_id=settings.default_tenant_id)
    mcp_app = module.http_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with module.mcp.session_manager.run():
            if store is not None:
                await store.init()
            await bus.connect()
            log.info(
                "tasks service ready",
                provider=provider.provider_name(),
                tenant=settings.default_tenant_id,
            )
            try:
                yield
            finally:
                await bus.close()
                if engine is not None:
                    await engine.dispose()

    app = FastAPI(title=MODULE_NAME, lifespan=lifespan)
    add_ops_routes(app, service_name=MODULE_NAME, version=_service_version())
    add_manifest_route(app, module)

    @app.get("/status")
    async def get_status() -> dict[str, Any]:
        """Provider and connection status for the manifest UI status panel."""
        return {
            "provider": provider.provider_name(),
        }

    app.mount("/mcp", mcp_app)
    return app


app = create_app()
