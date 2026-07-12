"""Runnable notes service: ops endpoints, the editor + review + attach page surfaces, and
the MCP app (structure + write tools; no read — notes are private), with Postgres + Qdrant
+ the platform API wired for the lifetime of the process."""

from __future__ import annotations

import asyncio
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
from epicurus_notes.db import NoteFolderStore, NotesStore
from epicurus_notes.indexer import NotesIndexer
from epicurus_notes.mirror import NotesMirror
from epicurus_notes.pages import NotesPages, create_pages_router
from epicurus_notes.service import MODULE_NAME, SAVED_SUBJECT, build_module
from epicurus_notes.settings import NotesSettings
from epicurus_notes.suggestions import (
    NoteSuggestionAuditStore,
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
    # module=MODULE_NAME so the platform client can read this module's suggestions setting.
    platform = PlatformClient(base_url=settings.platform_url, tenant_id=tenant, module=MODULE_NAME)
    indexer = NotesIndexer(
        qdrant,
        platform,
        tenant=tenant,
        chunk_max_chars=settings.chunk_max_chars,
    )

    bus = EventBus.from_settings(settings)
    suggestion_store = NoteSuggestionStore(engine)
    # Resolved-decision audit trail (ADR-0090): proposal vs. what was actually approved.
    suggestion_audit = NoteSuggestionAuditStore(engine)
    folders = NoteFolderStore(engine)

    async def _on_saved(slug: str) -> None:
        """Announce a saved note on NATS (tenant-scoped) for downstream consumers."""
        await bus.publish(SAVED_SUBJECT, {"slug": slug}, tenant_id=tenant)

    # Tenant-scope the file tree (constraint #1): <files-root>/<tenant>/notes. The mirror now
    # writes through the core file API (ADR-0065) — notes mounts no shared volume; notes_root
    # survives only as the base for the mirror's path-confinement arithmetic, and core_prefix
    # ("notes") maps a confined target to its core path (the core FileStore root is the tenant
    # segment, so the mirror is the "notes" subtree under it).
    notes_root = settings.notes_root.parent / tenant / settings.notes_root.name
    mirror = NotesMirror(
        notes_root, store, tenant=tenant, platform=platform, core_prefix=settings.notes_root.name
    )
    pages = NotesPages(
        store, indexer, tenant=tenant, on_saved=_on_saved, mirror=mirror, folders=folders
    )
    attachments = NotesAttachments(store, tenant=tenant)
    # Applies/discards agent-proposed note changes on the operator's word (ADR-0033).
    review = NoteSuggestionReview(
        suggestion_store, pages, store, tenant=tenant, audit=suggestion_audit
    )
    # build_module takes the review + platform so its propose tools can auto-apply when the
    # operator has turned review off (#KB-refactor).
    module = build_module(store, suggestion_store, review, platform, tenant=tenant)
    mcp_app = module.http_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with module.mcp.session_manager.run():
            await store.init()
            await suggestion_store.init()
            await suggestion_audit.init()
            await folders.init()
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

    async def _force_reindex() -> None:
        """Re-embed every note from scratch (#332): read all notes from the store and rebuild
        the ``<tenant>__notes`` collection with the current embedding model."""
        records = await store.list_all(tenant=tenant)
        await indexer.reindex([(r.slug, r.content) for r in records])

    # Holds the detached re-embed task so it isn't garbage-collected mid-run (RUF006).
    reindex_tasks: set[asyncio.Task[None]] = set()

    @app.post("/reindex")
    async def reindex_all() -> dict[str, str]:
        """Re-embed every note with the current embedding model (#332).

        The core's re-embed fan-out calls this when the operator changes the embedding model —
        vectors built with the old model are incompatible. Runs in the background.
        """
        task = asyncio.create_task(_force_reindex())
        reindex_tasks.add(task)
        task.add_done_callback(reindex_tasks.discard)
        return {"status": "started"}

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
