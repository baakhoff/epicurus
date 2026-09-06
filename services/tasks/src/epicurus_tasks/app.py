"""Runnable tasks service: ops endpoints + the MCP tool surface over HTTP."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from typing import Any, cast

from fastapi import FastAPI, HTTPException, Request
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_core import (
    EventBus,
    PlatformClient,
    add_manifest_route,
    add_ops_routes,
    add_portability_routes,
    configure_logging,
    get_logger,
)
from epicurus_tasks.db import RepeatStore, TaskStore
from epicurus_tasks.google_provider import GoogleTasksError, GoogleTasksProvider
from epicurus_tasks.lead_time_prefs import LeadTimePrefsStore
from epicurus_tasks.local_provider import LocalTasksProvider
from epicurus_tasks.models import Task, TaskScope
from epicurus_tasks.portability import TasksPortability
from epicurus_tasks.providers import TasksProvider
from epicurus_tasks.router import TasksRouter, operator_clock
from epicurus_tasks.scheduler import FiredMarkerStore, run_periodic
from epicurus_tasks.service import (
    BACKLOG_SHOW,
    MODULE_NAME,
    TASK_KIND,
    TASKS_PAGE_ID,
    TaskNotFound,
    build_module,
    build_tasks_backlog,
    build_tasks_board,
    calendar_feed_items,
    coerce_group,
    coerce_show,
    coerce_view,
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
    # Side table for emulated recurrence rules on external-provider tasks (ADR-0082); shares
    # the engine, so ``TaskStore.init`` provisions its table too. Google has no recurrence
    # field, so its repeating tasks' rules live here; the local store keeps its own in-row.
    repeats = RepeatStore(engine)
    # The lead-time scheduler's own tables (#664) — same engine/database, separate tables.
    lead_prefs = LeadTimePrefsStore(engine)
    markers = FiredMarkerStore(engine)

    bus = EventBus.from_settings(settings)
    # Hold every backend at once and route to the active list per the operator's selection
    # (ADR-0030): the silent local default plus each connectable external provider. There
    # is no longer a single provider chosen at startup.
    local_provider = LocalTasksProvider(store)
    external: dict[str, TasksProvider] = {
        "google": GoogleTasksProvider(platform=platform, repeats=repeats)
    }
    # The recurrence clock reads the operator's timezone (ADR-0039), not UTC, so the overdue
    # sweep and materialization treat "today" the way the operator does (#433, #535). Held as a
    # local so the board's Today/Overdue grouping (`page()` below) reads the *same* clock as the
    # sweep — otherwise the display groups by the UTC day while the sweep uses the operator's, and
    # the two disagree within one render across the UTC/operator midnight (a task the sweep counts
    # as due today lands in the board's Overdue column, #555). The lead-time scheduler (#664)
    # reuses this exact clock too, so it can never disagree with the sweep about what day it is.
    operator_today = operator_clock(platform.get_timezone)
    provider: TasksProvider = TasksRouter(
        local=local_provider,
        external=external,
        prefs=platform,
        now=operator_today,
        bus=bus,
    )

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

    module = build_module(
        provider, tenant_id=settings.default_tenant_id, categories=_list_categories
    )
    mcp_app = module.http_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with module.mcp.session_manager.run():
            # The local store always backs the module now (it is the silent default).
            await store.init()
            await lead_prefs.init()
            await markers.init()
            await bus.connect()
            # The lead-time scheduler (#664) — tasks' first periodic background job, the same
            # pattern as calendar's. Started/cancelled around the app lifetime.
            scheduler_task = asyncio.create_task(
                run_periodic(
                    tenant=settings.default_tenant_id,
                    provider=provider,
                    lead_prefs=lead_prefs,
                    markers=markers,
                    bus=bus,
                    today=operator_today,
                    poll_interval_s=settings.scheduler_poll_interval_s,
                )
            )
            log.info("tasks service ready", tenant=settings.default_tenant_id)
            try:
                yield
            finally:
                scheduler_task.cancel()
                with suppress(asyncio.CancelledError):
                    await scheduler_task
                await bus.close()
                await engine.dispose()

    app = FastAPI(title=MODULE_NAME, lifespan=lifespan)
    add_ops_routes(app, service_name=MODULE_NAME, version=_service_version())
    add_manifest_route(app, module)
    # Tenant data portability (#867/#871): GET /export + POST /import over the module's own
    # source-of-truth rows (tasks, external-provider repeat rules, the lead-time pref).
    portability = TasksPortability(tasks=store, repeats=repeats, lead_prefs=lead_prefs)
    add_portability_routes(app, module, portability)

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
        """Serve the Tasks page's `board` data (ADR-0018/0036/0047/#766/#820); core-proxied.

        Tasks from every enabled list are aggregated, then **partitioned by due date**
        (#766) via the ``show`` **view control**, folded onto this one page in #820 (a
        second ``can`` page/route no longer exists — ``/pages/can`` now 404s like any
        other unknown id). ``show`` is clamped by ``coerce_show`` to one of
        open/done/all/**backlog**: the first three fetch that :class:`TaskScope` and
        render the dated board via ``build_tasks_board`` (no "No date" bucket); ``backlog``
        is a *page-level* partition, not a widened ``TaskScope`` — it is branched on here,
        **before** the provider fetch, to fetch every status (``"all"``) and render the
        undated backlog via ``build_tasks_backlog`` instead, which splits completed undated
        tasks into their own muted section rather than needing a second, independent status
        filter (same fetch either way — two read views). Each card is tagged with its list
        (category). The other **view controls** drive forwarded query params (ADR-0049):
        ``group`` picks the board's column layout (due / status / priority / list / none;
        omitted under ``show=backlog`` — a flat backlog has nothing to group, #767's
        dead-knob rule) and ``view`` the client-side representation (board / list /
        calendar, #767 — echoed so the *View* switcher shows the active choice and *Group
        by* hides off the Board view). ``view=calendar`` has nothing to place a backlog on,
        so ``coerce_show`` corrects a ``show=backlog`` there back to the default *before*
        the branch above ever sees it — the Show control's own options drop ``backlog``
        under Calendar too, so the two always agree. A single failing list is skipped
        inside the router (#209), so the page degrades rather than blanking; the
        ``(GoogleTasksError, ValueError) → 502`` is a backstop. The Add forms offer a
        picker of the operator's enabled writable lists.
        """
        if page_id != TASKS_PAGE_ID:
            raise HTTPException(status_code=404, detail=f"no page {page_id!r}")
        tenant = settings.default_tenant_id
        view = coerce_view(request.query_params.get("view"))
        show = coerce_show(request.query_params.get("show"), view=view)
        # `backlog` fetches every status rather than a single TaskScope (#820) — see
        # build_tasks_backlog's docstring for why that's what keeps a completed undated
        # task reachable without a second, independent Show filter.
        scope: TaskScope = "all" if show == BACKLOG_SHOW else cast(TaskScope, show)
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
        # Group by the operator's day, not UTC's, using the *same* clock the sweep ran on this
        # request (#555). Core unreachable → this degrades to UTC exactly like the sweep's own
        # fallback (`operator_clock` → `_resolve_timezone`), so the two never disagree.
        today = await operator_today()
        if show == BACKLOG_SHOW:
            return build_tasks_backlog(
                tasks,
                today=today,
                view=view,
                lists=lists,
                default_list_id=default_list_id,
            )
        return build_tasks_board(
            tasks,
            today=today,
            view=view,
            group_by=coerce_group(request.query_params.get("group")),
            scope=show,
            lists=lists,
            default_list_id=default_list_id,
        )

    @app.get("/calendar-feed")
    async def calendar_feed(start: str, end: str) -> list[dict[str, Any]]:
        """Open tasks due within ``[start, end)`` — the calendar page's overlay (#469).

        Probed generically by the core's cross-module calendar-feed aggregator
        (``ModuleRegistry.calendar_feed_items``); this path has no manifest
        declaration, so a module that doesn't implement it simply 404s and is
        skipped. ``end`` is exclusive, matching the calendar archetype's own range
        contract (ADR-0023). Reuses the same tenant/provider path as the board
        (``scope="open"``), so a down/erroring list degrades the same way (#209)
        rather than failing the whole feed.
        """
        tenant = settings.default_tenant_id
        try:
            tasks = await provider.list_tasks(tenant, scope="open")
        except (GoogleTasksError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return calendar_feed_items(tasks, start, end)

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
