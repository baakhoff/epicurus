"""Runnable calendar service: ops endpoints + manifest + MCP tool surface."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from typing import Any

from fastapi import FastAPI, HTTPException
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_calendar.db import LocalEventStore
from epicurus_calendar.providers.google import GoogleCalendarProvider
from epicurus_calendar.providers.local import LocalCalendarProvider
from epicurus_calendar.service import (
    CALENDAR_PAGE_ID,
    MODULE_NAME,
    build_module,
    calendar_page,
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
    )

    engine = create_async_engine(settings.database_url)
    store = LocalEventStore(engine)

    if settings.calendar_provider == "google":
        provider: LocalCalendarProvider | GoogleCalendarProvider = GoogleCalendarProvider(
            platform=platform,
            calendar_id=settings.calendar_google_id,
        )
    else:
        provider = LocalCalendarProvider(store=store)

    bus = EventBus.from_settings(settings)
    module = build_module(provider, tenant_id=settings.default_tenant_id)
    mcp_app = module.http_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with module.mcp.session_manager.run():
            if settings.calendar_provider == "local":
                await store.init()
            await bus.connect()
            log.info(
                "calendar service ready",
                provider=settings.calendar_provider,
                tenant=settings.default_tenant_id,
            )
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
        """Live status for the manifest-driven UI status panel."""
        available = await provider.is_available(tenant_id=settings.default_tenant_id)
        status: dict[str, Any] = {
            "provider": provider.name,
            "available": available,
        }
        if settings.calendar_provider == "local":
            status["event_count"] = await store.count(tenant=settings.default_tenant_id)
        return status

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
        try:
            return await calendar_page(
                provider,
                tenant_id=settings.default_tenant_id,
                start=start,
                end=end,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    app.mount("/mcp", mcp_app)

    return app


app = create_app()
