"""Runnable calendar service: ops endpoints + manifest + MCP tool surface."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from typing import Any

from fastapi import FastAPI, HTTPException
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_calendar.db import LocalEventStore
from epicurus_calendar.lead_time_prefs import LeadTimePrefsStore
from epicurus_calendar.models import Event
from epicurus_calendar.providers.base import CalendarProvider
from epicurus_calendar.providers.google import GoogleCalendarProvider
from epicurus_calendar.providers.local import LocalCalendarProvider
from epicurus_calendar.providers.router import CollectionRouter
from epicurus_calendar.scheduler import FiredMarkerStore, run_periodic
from epicurus_calendar.service import (
    CALENDAR_PAGE_ID,
    EVENT_KIND,
    MODULE_NAME,
    EventNotFound,
    build_calendar_choices,
    build_module,
    calendar_accounts,
    calendar_attachments,
    calendar_page,
    event_attachment,
    event_hover_card,
    fetch_event,
    resolve_timezone,
)
from epicurus_calendar.settings import CalendarSettings
from epicurus_core import (
    EventBus,
    PlatformClient,
    add_manifest_route,
    add_ops_routes,
    configure_logging,
    get_logger,
)


def _service_version() -> str:
    try:
        return pkg_version("epicurus-calendar")
    except PackageNotFoundError:
        return "0.0.0"


def create_app() -> FastAPI:
    """Build the calendar ASGI app."""
    settings = CalendarSettings(service_name=MODULE_NAME)
    configure_logging(settings)
    log = get_logger(MODULE_NAME)

    platform = PlatformClient(
        base_url=settings.platform_url,
        tenant_id=settings.default_tenant_id,
        module=MODULE_NAME,
    )

    engine = create_async_engine(settings.database_url)
    store = LocalEventStore(engine)
    # The lead-time scheduler's own tables (#664) — same engine/database as the event store,
    # separate tables (the module owns all three, no shared-DB coupling with another service).
    lead_prefs = LeadTimePrefsStore(engine)
    markers = FiredMarkerStore(engine)

    bus = EventBus.from_settings(settings)
    # Hold every backend at once and route per the operator's selection (ADR-0030): the
    # silent local default plus each connectable external provider. The router reads the
    # stored enabled/active collections from the core via the PlatformClient — there is no
    # longer a single provider chosen at startup. `bus` wires the event-spine emission seam
    # (#664) directly into the router, the one place every write already passes through.
    local_provider = LocalCalendarProvider(store=store)
    external: dict[str, CalendarProvider] = {"google": GoogleCalendarProvider(platform=platform)}
    provider = CollectionRouter(local=local_provider, external=external, prefs=platform, bus=bus)

    # Naive (offset-less) start/end inputs are read in the operator's configured
    # timezone (ADR-0039) rather than UTC, so "3 PM" is the operator's 3 PM (#433).
    module = build_module(
        provider, tenant_id=settings.default_tenant_id, timezone=platform.get_timezone
    )
    mcp_app = module.http_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with module.mcp.session_manager.run():
            # The local store always backs the module now (it is the silent default), so
            # it is always initialised — not only when "local" was the chosen provider.
            await store.init()
            await lead_prefs.init()
            await markers.init()
            await bus.connect()
            # The lead-time scheduler (#664) — calendar's first periodic background job.
            # Started/cancelled around the app lifetime like knowledge's vault watcher.
            scheduler_task = asyncio.create_task(
                run_periodic(
                    tenant=settings.default_tenant_id,
                    provider=provider,
                    lead_prefs=lead_prefs,
                    markers=markers,
                    bus=bus,
                    poll_interval_s=settings.scheduler_poll_interval_s,
                )
            )
            log.info("calendar service ready", tenant=settings.default_tenant_id)
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

    async def _require_event(ref_id: str) -> Event:
        """Fetch a referenced event, translating a miss into a 404 for the proxy."""
        try:
            return await fetch_event(provider, tenant_id=settings.default_tenant_id, ref_id=ref_id)
        except EventNotFound as exc:
            raise HTTPException(status_code=404, detail=f"event {ref_id!r} not found") from exc

    @app.get("/status")
    async def get_status() -> dict[str, Any]:
        """Live status for the manifest-driven UI status panel.

        Reports whether Google is connected (best-effort), the Google Calendar timezone
        (when connected — the core's ``now`` tool reads it to flag a mismatch, ADR-0039),
        and how many events the local default store holds; the operator's chosen calendars
        are shown in the connected-accounts section rather than here (ADR-0030).
        """
        tenant = settings.default_tenant_id
        connected = await external["google"].is_available(tenant_id=tenant)
        google_timezone = (
            await external["google"].get_timezone(tenant_id=tenant) if connected else None
        )
        return {
            "google_connected": connected,
            "google_timezone": google_timezone,
            "local_events": await store.count(tenant=tenant),
        }

    @app.get("/accounts")
    async def get_accounts() -> dict[str, Any]:
        """Connected accounts + their calendars for the operator's picker (ADR-0030).

        The core proxies this at ``GET /platform/v1/modules/calendar/collections`` and
        folds in the stored enabled/active selection before the shell renders it.
        """
        view = await calendar_accounts(external, tenant_id=settings.default_tenant_id)
        return view.model_dump()

    @app.get("/pages/{page_id}")
    async def get_page(
        page_id: str, start: str | None = None, end: str | None = None
    ) -> dict[str, Any]:
        """Serve the calendar archetype's page data (ADR-0018); the core proxies this.

        ``start``/``end`` (ISO-8601) bound the window the shell is viewing — the core
        forwards them from ``GET /platform/v1/modules/calendar/pages/{page_id}``. When
        absent, the page falls back to the current month.
        """
        if page_id != CALENDAR_PAGE_ID:
            raise HTTPException(status_code=404, detail=f"no page {page_id!r}")
        # Offer the writable calendars as the New-event picker and preselect the active one.
        # Best-effort: a prefs/discovery hiccup degrades to local-only, never a page error.
        active = None
        try:
            active = (await platform.get_collections()).active
        except Exception:  # picker is optional; the page must still render
            active = None
        calendars, active_token = await build_calendar_choices(
            external, tenant_id=settings.default_tenant_id, active=active
        )
        try:
            return await calendar_page(
                provider,
                tenant_id=settings.default_tenant_id,
                start=start,
                end=end,
                calendars=calendars,
                active_token=active_token,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/resolve/{kind}/{ref_id}")
    async def resolve_entity(kind: str, ref_id: str) -> dict[str, Any]:
        """Resolve a referenced event to a core hover-card (ADR-0019); core-proxied.

        Calendar only references events; an unknown *kind* is a 404. Returns the
        uniform HoverCard envelope the shell renders inline and in the entity-detail
        panel — start/end (in the operator's timezone, named, #559), location, and the
        calendar (provider) name.
        """
        if kind != EVENT_KIND:
            raise HTTPException(status_code=404, detail=f"unknown entity kind {kind!r}")
        tz, tz_name = await resolve_timezone(platform.get_timezone)
        return event_hover_card(await _require_event(ref_id), tz=tz, tz_name=tz_name)

    @app.get("/attachments")
    async def list_attachments() -> list[dict[str, str]]:
        """Chat-attachment picker (ADR-0019): upcoming events the composer can attach."""
        return await calendar_attachments(provider, tenant_id=settings.default_tenant_id)

    @app.get("/attachments/{ref_id}")
    async def get_attachment(ref_id: str) -> dict[str, str]:
        """Resolve an attached event to the text the agent injects (ADR-0019)."""
        tz, tz_name = await resolve_timezone(platform.get_timezone)
        return event_attachment(await _require_event(ref_id), tz=tz, tz_name=tz_name)

    app.mount("/mcp", mcp_app)

    return app


app = create_app()
