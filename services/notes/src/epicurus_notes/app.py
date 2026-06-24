"""Runnable notes service: ops endpoints, the editor + review + attach page surfaces, and
the MCP app (structure + write tools; no read — notes are private), with Postgres + Qdrant
+ the platform API wired for the lifetime of the process."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from typing import Any

from fastapi import FastAPI
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_core import (
    EventBus,
    PlatformClient,
    add_manifest_route,
    add_ops_routes,
    configure_logging,
    get_logger,
)
from epicurus_notes.attachments import NotesAttachments, create_attachments_router
from epicurus_notes.db import NotesStore
from epicurus_notes.indexer import NotesIndexer
from epicurus_notes.mirror import NotesMirror
from epicurus_notes.pages import NotesPages, create_pages_router
from epicurus_notes.service import MODULE_NAME, SAVED_SUBJECT, build_module
from epicurus_notes.settings import NotesSettings
from epicurus_notes.suggestions import (
    NoteSuggestionReview,
    NoteSuggestionStore,
    create_note_review_router,
)


def _service_version() -> str:
    try:
        return pkg_version("epicurus-notes")
    except PackageNotFoundError:
        return "0.0.0"


def create_app() -> FastAPI:
    """Build the notes ASGI app (connects at startup)."""
    settings = NotesSettings(service_name=MODULE_NAME)
    configure_logging(settings)
    log = get_logger(MODULE_NAME)

    tenant = settings.default_tenant_id
    engine = create_async_engine(settings.database_url)
    store = NotesStore(engine)
    qdrant = AsyncQdrantClient(url=settings.qdrant_url)
    platform = PlatformClient(base_url=settings.platform_url, tenant_id=tenant)
    indexer = NotesIndexer(
        qdrant,
        platform,
        tenant=tenant,
        chunk_max_chars=settings.chunk_max_chars,
    )

    bus = EventBus.from_settings(settings)
    suggestion_store = NoteSuggestionStore(engine)
    # The agent's write tools stage suggestions; build_module registers them + the pages.
    module = build_module(store, suggestion_store, tenant=tenant)
    mcp_app = module.http_app()

    async def _on_saved(slug: str) -> None:
        """Announce a saved note on NATS (tenant-scoped) for downstream consumers."""
        await bus.publish(SAVED_SUBJECT, {"slug": slug}, tenant_id=tenant)

    mirror = NotesMirror(settings.notes_root, store, tenant=tenant)
    pages = NotesPages(store, indexer, tenant=tenant, on_saved=_on_saved, mirror=mirror)
    attachments = NotesAttachments(store, tenant=tenant)
    # Applies/discards agent-proposed note changes on the operator's word (ADR-0033).
    review = NoteSuggestionReview(suggestion_store, pages, store, tenant=tenant)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with module.mcp.session_manager.run():
            await store.init()
            await suggestion_store.init()
            # One-time copy of pre-existing notes into the shared file space (#KB-refactor).
            await mirror.backfill()
            await bus.connect()
            log.info("notes service ready", tenant=tenant)
            try:
                yield
            finally:
                await bus.close()
                await engine.dispose()
                await qdrant.close()

    app = FastAPI(title=MODULE_NAME, lifespan=lifespan)
    add_ops_routes(app, service_name=MODULE_NAME, version=_service_version())
    add_manifest_route(app, module)

    # The review page (#KB-refactor): registered BEFORE the editor pages router so its
    # literal /pages/review route wins over the editor's /pages/{page_id} path param.
    app.include_router(create_note_review_router(review))
    # The editor page (#134) and the chat-attachment surface — the shell renders both;
    # this module supplies note data only (ADR-0018 / ADR-0019).
    app.include_router(create_pages_router(pages))
    app.include_router(create_attachments_router(attachments))

    @app.get("/status")
    async def get_status() -> dict[str, Any]:
        """Note statistics for the manifest-driven UI status panel."""
        return {
            "note_count": await store.count(tenant=tenant),
            "last_updated_at": await store.last_updated_at(tenant=tenant),
        }

    app.mount("/mcp", mcp_app)

    return app


app = create_app()
