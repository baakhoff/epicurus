"""Runnable knowledge service: ops endpoints + MCP tool surface."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
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
from epicurus_knowledge.attachments import VaultAttachments, create_attachments_router
from epicurus_knowledge.db import DocIndex, NoteIndex
from epicurus_knowledge.indexer import KnowledgeIndexer
from epicurus_knowledge.module_docs import ModuleDocLedger, ModuleDocsIndexer
from epicurus_knowledge.pages import VaultPages, create_pages_router
from epicurus_knowledge.resolver import KnowledgeResolver, create_resolver_router
from epicurus_knowledge.runner import IndexRunner
from epicurus_knowledge.service import MODULE_NAME, build_module, module_docs
from epicurus_knowledge.settings import KnowledgeSettings
from epicurus_knowledge.suggestions import (
    SuggestionReview,
    SuggestionStore,
    create_review_router,
)
from epicurus_knowledge.watcher import VaultWatcher


def _service_version() -> str:
    try:
        return pkg_version("epicurus-knowledge")
    except PackageNotFoundError:
        return "0.0.0"


def create_app() -> FastAPI:
    """Build the knowledge ASGI app (connects at startup)."""
    settings = KnowledgeSettings(service_name=MODULE_NAME)
    configure_logging(settings)
    log = get_logger(MODULE_NAME)

    engine = create_async_engine(settings.database_url)
    note_index = NoteIndex(engine)
    doc_index = DocIndex(engine)
    module_doc_ledger = ModuleDocLedger(engine)
    suggestion_store = SuggestionStore(engine)
    qdrant = AsyncQdrantClient(url=settings.qdrant_url)
    platform = PlatformClient(
        base_url=settings.platform_url,
        tenant_id=settings.default_tenant_id,
        module=MODULE_NAME,  # so the indexer can resolve its embedding-model slot (#128)
    )

    vault_indexer = KnowledgeIndexer(
        note_index,
        qdrant,
        platform,
        vault_path=settings.vault_path,
        tenant=settings.default_tenant_id,
        collection_base="knowledge",
        chunk_max_chars=settings.chunk_max_chars,
        embed_batch_size=settings.embed_batch_size,
    )
    docs_indexer = KnowledgeIndexer(
        doc_index,
        qdrant,
        platform,
        vault_path=settings.docs_path,
        tenant=settings.default_tenant_id,
        collection_base="docs",
        chunk_max_chars=settings.chunk_max_chars,
        embed_batch_size=settings.embed_batch_size,
    )
    # Per-module docs (#215): indexed alongside the bundled platform docs into
    # <tenant>__docs; synced at startup and on every knowledge_reindex call.
    module_docs_indexer = ModuleDocsIndexer(
        module_doc_ledger,
        qdrant,
        platform,
        tenant=settings.default_tenant_id,
        chunk_max_chars=settings.chunk_max_chars,
    )

    bus = EventBus.from_settings(settings)
    # Watch mode (#232, ADR-0035) marks the vault externally owned: the editor goes
    # read-only and agent suggestions can't be applied, so Obsidian stays the sole author.
    vault_read_only = settings.vault_read_only
    vault_pages = VaultPages(
        settings.vault_path,
        vault_indexer,
        read_only=vault_read_only,
        # Surface the bundled platform docs as the read-only "__docs__" scope (#KB-refactor).
        docs_path=settings.docs_path,
    )
    suggestion_review = SuggestionReview(
        suggestion_store,
        vault_pages,
        vault_indexer,
        vault_path=settings.vault_path,
        tenant=settings.default_tenant_id,
        read_only=vault_read_only,
    )
    # build_module takes the review + platform so its propose tools can auto-apply when the
    # operator has turned review off (#KB-refactor).
    module = build_module(
        vault_indexer,
        docs_indexer,
        module_docs_indexer,
        suggestion_store,
        suggestion_review,
        platform,
        tenant=settings.default_tenant_id,
        vault_path=settings.vault_path,
    )
    mcp_app = module.http_app()

    # The initial index runs in the background (#230): the vault + bundled docs can
    # take minutes, so blocking startup would make /health flap and could trip restart
    # loops. The runner retries with backoff so a cold `compose up` (deps not yet
    # reachable) still ends with a populated index; GET /status reports its progress.
    index_runner = IndexRunner(
        [vault_indexer, docs_indexer, module_docs_indexer],
        max_attempts=settings.index_retry_max_attempts,
        base_delay_seconds=settings.index_retry_base_delay_seconds,
        max_delay_seconds=settings.index_retry_max_delay_seconds,
    )

    # Live vault sync (#232): when an externally-synced vault is mounted, watch it and
    # re-index incrementally on change. Off by default; the indexer's run-lock serialises
    # a watch pass against the startup index, so the two never walk the vault at once.
    vault_watcher = (
        VaultWatcher(
            settings.vault_path,
            vault_indexer,
            debounce_ms=settings.vault_watch_debounce_ms,
        )
        if settings.vault_watch
        else None
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with module.mcp.session_manager.run():
            await note_index.init()
            await doc_index.init()
            await module_doc_ledger.init()
            await suggestion_store.init()
            await bus.connect()
            log.info(
                "knowledge service ready",
                vault=str(settings.vault_path),
                docs=str(settings.docs_path),
                tenant=settings.default_tenant_id,
                vault_watch=settings.vault_watch,
                vault_read_only=vault_read_only,
            )
            # Serve immediately; index in the background.
            index_task = asyncio.create_task(index_runner.run_with_retry())
            watch_task = (
                asyncio.create_task(vault_watcher.run()) if vault_watcher is not None else None
            )
            try:
                yield
            finally:
                index_task.cancel()
                with suppress(asyncio.CancelledError):
                    await index_task
                if vault_watcher is not None and watch_task is not None:
                    vault_watcher.stop()
                    watch_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await watch_task
                await bus.close()
                await engine.dispose()
                await qdrant.close()

    app = FastAPI(title=MODULE_NAME, lifespan=lifespan)
    add_ops_routes(app, service_name=MODULE_NAME, version=_service_version())
    add_manifest_route(app, module)

    # The review page (#220): registered BEFORE the editor pages router so its literal
    # GET /pages/review route wins over the editor's GET /pages/{page_id} path param.
    app.include_router(create_review_router(suggestion_review))

    # The editor page (#130): the shell renders it; this module supplies vault docs.
    app.include_router(create_pages_router(vault_pages))

    # Attachment source (#137): pick a vault doc to attach to a chat turn as context.
    app.include_router(create_attachments_router(VaultAttachments(settings.vault_path)))

    # Hover-card resolver (#143): a cited vault note or platform doc → a HoverCard.
    app.include_router(
        create_resolver_router(
            KnowledgeResolver(
                vault_path=settings.vault_path,
                docs_path=settings.docs_path,
                note_index=note_index,
                doc_index=doc_index,
                tenant=settings.default_tenant_id,
            )
        )
    )

    @app.get("/module-docs")
    async def get_module_docs() -> dict[str, Any]:
        """Return this module's documentation pages for auto-indexing (#215).

        Served at ``/module-docs``, not ``/docs`` — the latter is FastAPI's built-in
        Swagger UI, which would shadow this route and return HTML.
        """
        return module_docs()

    @app.get("/status")
    async def get_status() -> dict[str, Any]:
        """Index statistics for the manifest-driven UI status panel.

        Counts grow as the background index (#230) progresses; ``index_phase`` and
        ``index_attempts`` report whether it is still running, ready, or retrying.
        """
        note_count = await note_index.count(tenant=settings.default_tenant_id)
        last_indexed_at = await note_index.last_indexed_at(tenant=settings.default_tenant_id)
        doc_count = await doc_index.count(tenant=settings.default_tenant_id)
        module_doc_count = await module_doc_ledger.count(tenant=settings.default_tenant_id)
        return {
            "note_count": note_count,
            "last_indexed_at": last_indexed_at,
            "doc_count": doc_count,
            "module_doc_count": module_doc_count,
            "index_phase": index_runner.state.phase,
            "index_attempts": index_runner.state.attempts,
        }

    app.mount("/mcp", mcp_app)

    return app


app = create_app()
