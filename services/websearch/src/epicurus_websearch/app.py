"""Runnable websearch service: ops endpoints + MCP tool surface."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI

from epicurus_core import (
    EventBus,
    HoverCard,
    HoverCardDetail,
    HoverCardLink,
    add_manifest_route,
    add_ops_routes,
    configure_logging,
    get_logger,
)
from epicurus_websearch.refs import decode_ref
from epicurus_websearch.searxng import SearXNGClient
from epicurus_websearch.service import MODULE_NAME, build_module
from epicurus_websearch.settings import WebSearchSettings


def _service_version() -> str:
    try:
        return pkg_version("epicurus-websearch")
    except PackageNotFoundError:
        return "0.0.0"


def create_app() -> FastAPI:
    """Build the websearch ASGI app."""
    settings = WebSearchSettings(service_name=MODULE_NAME)
    configure_logging(settings)
    log = get_logger(MODULE_NAME)

    client = SearXNGClient(
        base_url=settings.searxng_url,
        engines=settings.websearch_engines,
    )
    bus = EventBus.from_settings(settings)
    module = build_module(client, max_results=settings.websearch_max_results)
    mcp_app = module.http_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with module.mcp.session_manager.run():
            await bus.connect()
            log.info(
                "websearch service ready",
                searxng_url=settings.searxng_url,
                max_results=settings.websearch_max_results,
                tenant=settings.default_tenant_id,
            )
            try:
                yield
            finally:
                await bus.close()
                await client.aclose()

    app = FastAPI(title=MODULE_NAME, lifespan=lifespan)
    add_ops_routes(app, service_name=MODULE_NAME, version=_service_version())
    add_manifest_route(app, module)

    @app.get("/status")
    async def get_status() -> dict[str, Any]:
        """SearXNG reachability status for the manifest-driven UI status panel."""
        healthy = await client.health_check()
        return {"searxng_healthy": healthy, "searxng_url": settings.searxng_url}

    @app.get("/resolve/result/{ref_id}", response_model=HoverCard)
    async def resolve_result(ref_id: str) -> HoverCard:
        """Hover-card resolver for a web-search result entity (#551, ADR-0019).

        Stateless: the module holds no store, so every field is reconstructed
        from the self-describing ``ref_id`` (``epicurus_websearch.refs``)
        rather than a lookup — a session reopened days after the search still
        resolves. Unlike mail's in-app precedent, this always carries an
        ``href``: a web-search result's only destination is the page itself,
        opened in a new tab (the module has no right-panel view of its own).
        """
        result = decode_ref(ref_id)
        domain = urlsplit(result["url"]).netloc
        return HoverCard(
            title=result["title"],
            description=result["snippet"],
            details=[
                HoverCardDetail(label="Engine", value=result["engine"]),
                HoverCardDetail(label="Domain", value=domain),
            ],
            href=HoverCardLink(label="Open page", url=result["url"]),
        )

    app.mount("/mcp", mcp_app)

    return app


app = create_app()
