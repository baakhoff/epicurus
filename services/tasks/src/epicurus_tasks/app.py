"""Runnable tasks service: ops endpoints + the MCP tool surface over HTTP."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from typing import Any

from fastapi import FastAPI, HTTPException
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
from epicurus_tasks.google_provider import GoogleTasksError, GoogleTasksProvider
from epicurus_tasks.local_provider import LocalTasksProvider
from epicurus_tasks.models import Task
from epicurus_tasks.providers import TasksProvider
from epicurus_tasks.service import (
    MODULE_NAME,
    TASKS_PAGE_ID,
    TaskNotFound,
    build_module,
    build_tasks_board,
    fetch_task,
    task_attachment,
    tasks_attachments,
)
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

    @app.get("/pages/{page_id}")
    async def page(page_id: str) -> dict[str, Any]:
        """Serve the Tasks page's `board` data (ADR-0018); the core proxies this.

        Open tasks are grouped into due-date columns by ``build_tasks_board``. A
        provider failure (e.g. Google not connected) becomes a 502 carrying the
        reason, so the shell can show it instead of a blank board.
        """
        if page_id != TASKS_PAGE_ID:
            raise HTTPException(status_code=404, detail=f"no page {page_id!r}")
        try:
            tasks = await provider.list_tasks(settings.default_tenant_id)
        except (GoogleTasksError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        today = datetime.now(UTC).date().isoformat()
        return build_tasks_board(tasks, today=today)

    async def _require_task(ref_id: str) -> Task:
        """Fetch an attached/referenced task, translating misses into proxy errors.

        A missing task is a ``404``; a provider failure (e.g. Google not connected)
        is a ``502`` carrying the reason — the same contract the page endpoint uses.
        """
        try:
            return await fetch_task(provider, tenant_id=settings.default_tenant_id, ref_id=ref_id)
        except TaskNotFound as exc:
            raise HTTPException(status_code=404, detail=f"task {ref_id!r} not found") from exc
        except (GoogleTasksError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/attachments")
    async def list_attachments() -> list[dict[str, str]]:
        """Chat-attachment picker (ADR-0019): open tasks the composer can attach."""
        try:
            return await tasks_attachments(provider, tenant_id=settings.default_tenant_id)
        except (GoogleTasksError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/attachments/{ref_id}")
    async def get_attachment(ref_id: str) -> dict[str, str]:
        """Resolve an attached task to the text the agent injects (ADR-0019)."""
        return task_attachment(await _require_task(ref_id))

    app.mount("/mcp", mcp_app)
    return app


app = create_app()
