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
    PlatformClient,
    add_manifest_route,
    add_ops_routes,
    configure_logging,
    get_logger,
)
from epicurus_websearch.ingest import LinkIngestor
from epicurus_websearch.refs import decode_ref, decode_source_ref
from epicurus_websearch.safety import FetchLimits, GuardedFetcher, UrlGuard
from epicurus_websearch.searxng import SearXNGClient
from epicurus_websearch.service import MODULE_NAME, build_module, describe_unresponsive
from epicurus_websearch.settings import WebSearchSettings
from epicurus_websearch.vision import VisionCaptioner


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
    # link_ingest (#739): a guarded fetcher for operator-supplied URLs, and a captioner that
    # asks the *core* to describe images — the module holds no model keys (constraint #8).
    fetcher = GuardedFetcher(
        guard=UrlGuard(),
        limits=FetchLimits(
            max_bytes=settings.link_ingest_max_bytes,
            timeout_s=settings.link_ingest_timeout_s,
            max_redirects=settings.link_ingest_max_redirects,
            user_agent=settings.link_ingest_user_agent,
        ),
    )
    ingestor = LinkIngestor(
        fetcher=fetcher,
        captioner=VisionCaptioner(
            PlatformClient(
                base_url=settings.platform_url,
                tenant_id=settings.default_tenant_id,
                module=MODULE_NAME,
            ),
            model=settings.link_ingest_vision_model,
        ),
        max_text_chars=settings.link_ingest_max_text_chars,
        use_media_probe=settings.link_ingest_ytdlp,
    )
    module = build_module(client, max_results=settings.websearch_max_results, ingestor=ingestor)
    mcp_app = module.http_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with module.mcp.session_manager.run():
            await bus.connect()
            log.info(
                "websearch service ready",
                searxng_url=settings.searxng_url,
                max_results=settings.websearch_max_results,
                ingest_max_bytes=settings.link_ingest_max_bytes,
                ingest_ytdlp=settings.link_ingest_ytdlp,
                tenant=settings.default_tenant_id,
            )
            try:
                yield
            finally:
                await bus.close()
                await client.aclose()
                await fetcher.aclose()

    app = FastAPI(title=MODULE_NAME, lifespan=lifespan)
    add_ops_routes(app, service_name=MODULE_NAME, version=_service_version())
    add_manifest_route(app, module)

    @app.get("/status")
    async def get_status() -> dict[str, Any]:
        """SearXNG reachability *and* search-quality status for the Modules-page panel.

        ``searxng_healthy`` is liveness (``/healthz``, unchanged) — it answers ``true`` as
        long as the SearXNG process itself is up, even when every engine it asks is
        blocked or rate-limited (#920's exact failure mode). ``degraded`` and
        ``unresponsive_engines`` report the *last actual search's* engine health instead of
        a separate canary query: a canary would mean the module polling SearXNG's own
        (possibly rate-limited) engines on an interval purely to test them, which competes
        with real traffic for the same limited budget on the engines already flagged as the
        problem — the operator's own use of the tool is a free, always-fresh signal, and
        this module makes no other request to SearXNG anyway. The tradeoff: a degraded
        instance nobody has searched with since it broke still reads healthy here until the
        next search — acceptable for a health panel that exists to explain the *next*
        result, not to poll for outages independent of use. ``search_evidence`` is what
        keeps that honest: ``degraded: false`` on a freshly started instance is the absence
        of evidence, not evidence of health, and the panel says which it is looking at.
        """
        healthy = await client.health_check()
        unresponsive = client.last_unresponsive_engines
        # Flat scalars only: the core proxies this object verbatim and the Modules panel
        # renders each value with `String(v)`, so a nested list of objects would read as
        # "[object Object]" (`docs/reference/modules.md` — a status field is a flat value).
        return {
            "searxng_healthy": healthy,
            "searxng_url": settings.searxng_url,
            "search_evidence": (
                "a search has run since this instance started"
                if client.has_searched
                else "no search has run since this instance started"
            ),
            "degraded": bool(unresponsive),
            "unresponsive_engines": describe_unresponsive(unresponsive) or None,
        }

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

    @app.get("/resolve/source/{ref_id}", response_model=HoverCard)
    async def resolve_source(ref_id: str) -> HoverCard:
        """Hover-card resolver for an ingested link (#739, ADR-0019).

        Stateless in exactly the way the search-result resolver is: everything is decoded
        out of the self-describing ``ref_id``, so the chip on a months-old turn still
        resolves even though the module never stored the ingest. What differs is the
        details — a *kind* (article / image / video / page / unreachable) and the site,
        rather than the search engine that surfaced it.
        """
        source = decode_source_ref(ref_id)
        return HoverCard(
            title=source["title"],
            description=source["summary"],
            details=[
                HoverCardDetail(label="Kind", value=source["kind"]),
                HoverCardDetail(
                    label="Site", value=source["site"] or urlsplit(source["url"]).netloc
                ),
            ],
            href=HoverCardLink(label="Open page", url=source["url"]),
        )

    app.mount("/mcp", mcp_app)

    return app


app = create_app()
