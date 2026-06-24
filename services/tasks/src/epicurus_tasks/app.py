"""Runnable tasks service: ops endpoints + the MCP tool surface over HTTP."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from typing import Any

from fastapi import FastAPI, HTTPException, Request
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
from epicurus_tasks.router import TasksRouter
from epicurus_tasks.service import (
    MODULE_NAME,
    TASK_KIND,
    TASKS_PAGE_ID,
    TaskNotFound,
    build_module,
    build_tasks_board,
    coerce_group,
    coerce_scope,
    enabled_write_lists,
    fetch_task,
    task_attachment,
    task_hover_card,
    tasks_accounts,
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

    platform = PlatformClient(
        base_url=settings.platform_url,
        tenant_id=settings.default_tenant_id,
        module=MODULE_NAME,
    )

    engine = create_async_engine(settings.database_url)
    store = TaskStore(engine)

    # Hold every backend at once and route to the active list per the operator's selection
    # (ADR-0030): the silent local default plus each connectable external provider. There
    # is no longer a single provider chosen at startup.
    local_provider = LocalTasksProvider(store)
    external: dict[str, TasksProvider] = {"google": GoogleTasksProvider(platform=platform)}
    provider: TasksProvider = TasksRouter(local=local_provider, external=external, prefs=platform)

    async def _list_categories() -> list[tuple[str, str]]:
        """The enabled writable lists ``(id, title)`` for the ``tasks_lists`` tool.

        Best-effort: any prefs/discovery error yields ``[]`` so the tool degrades to
        "only the default list" rather than failing the agent.
        """
        try:
            prefs = await platform.get_collections()
            lists, _ = await enabled_write_lists(
                external, prefs, tenant_id=settings.default_tenant_id
            )
            return lists
        except Exception:  # a discovery hiccup must not break the tool
            return []

    bus = EventBus.from_settings(settings)
    module = build_module(
        provider, tenant_id=settings.default_tenant_id, categories=_list_categories
    )
    mcp_app = module.http_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with module.mcp.session_manager.run():
            # The local store always backs the module now (it is the silent default).
            await store.init()
            await bus.connect()
            log.info("tasks service ready", tenant=settings.default_tenant_id)
            try:
                yield
            finally:
                await bus.close()
                await engine.dispose()

    app = FastAPI(title=MODULE_NAME, lifespan=lifespan)
    add_ops_routes(app, service_name=MODULE_NAME, version=_service_version())
    add_manifest_route(app, module)

    @app.get("/status")
    async def get_status() -> dict[str, Any]:
        """Connection status for the manifest UI status panel.

        Reports whether Google is connected (best-effort); the operator's active task
        list is shown in the connected-accounts section rather than here (ADR-0030).
        """
        return {
            "google_connected": await external["google"].is_available(settings.default_tenant_id),
        }

    @app.get("/accounts")
    async def get_accounts() -> dict[str, Any]:
        """Connected accounts + their task lists for the operator's picker (ADR-0030).

        The core proxies this at ``GET /platform/v1/modules/tasks/collections`` and folds
        in the stored enabled/active selection before the shell renders it.
        """
        view = await tasks_accounts(external, tenant_id=settings.default_tenant_id)
        return view.model_dump()

    @app.get("/pages/{page_id}")
    async def page(page_id: str, request: Request) -> dict[str, Any]:
        """Serve the Tasks page's `board` data (ADR-0018/0036/0047); the core proxies this.

        Tasks from every enabled list are aggregated and grouped into columns by
        ``build_tasks_board``, each card tagged with its list (category). The board's
        **view controls** drive two forwarded query params (ADR-0049): ``group`` picks the
        column layout (due / status / priority / list / none) and ``show`` the task scope
        (open / completed / all) fetched from the providers; both are clamped to known
        values. A single failing list is skipped inside the router (#209), so the page
        degrades rather than blanking; the ``(GoogleTasksError, ValueError) → 502`` is a
        backstop. The Add form offers a picker of the operator's enabled writable lists.
        """
        if page_id != TASKS_PAGE_ID:
            raise HTTPException(status_code=404, detail=f"no page {page_id!r}")
        tenant = settings.default_tenant_id
        group_by = coerce_group(request.query_params.get("group"))
        scope = coerce_scope(request.query_params.get("show"))
        try:
            tasks = await provider.list_tasks(tenant, scope=scope)
        except (GoogleTasksError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        # The list picker must never fail the board: degrade to "no picker" on any error.
        lists: list[tuple[str, str]] = []
        default_list_id: str | None = None
        try:
            prefs = await platform.get_collections()
            lists, default_list_id = await enabled_write_lists(external, prefs, tenant_id=tenant)
        except Exception as exc:
            log.warning("tasks board: list picker unavailable", error=str(exc))
        today = datetime.now(UTC).date().isoformat()
        return build_tasks_board(
            tasks,
            today=today,
            group_by=group_by,
            scope=scope,
            lists=lists,
            default_list_id=default_list_id,
        )

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

    @app.get("/resolve/{kind}/{ref_id}")
    async def resolve_entity(kind: str, ref_id: str) -> dict[str, Any]:
        """Resolve a referenced task to a core hover-card (ADR-0019); core-proxied.

        Tasks only references tasks; an unknown *kind* is a 404. Returns the uniform
        HoverCard envelope the shell renders inline and in the entity-detail panel —
        the task's due date and open/completed status.
        """
        if kind != TASK_KIND:
            raise HTTPException(status_code=404, detail=f"unknown entity kind {kind!r}")
        return task_hover_card(await _require_task(ref_id))

    app.mount("/mcp", mcp_app)
    return app


app = create_app()
