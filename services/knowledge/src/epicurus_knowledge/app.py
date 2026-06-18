"""Runnable knowledge service: ops endpoints + MCP tool surface."""

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
from epicurus_knowledge.attachments import VaultAttachments, create_attachments_router
from epicurus_knowledge.db import DocIndex, NoteIndex
from epicurus_knowledge.indexer import KnowledgeIndexer
from epicurus_knowledge.module_docs import ModuleDocLedger, ModuleDocsIndexer
from epicurus_knowledge.pages import VaultPages, create_pages_router
from epicurus_knowledge.resolver import KnowledgeResolver, create_resolver_router
from epicurus_knowledge.service import MODULE_NAME, build_module, module_docs
from epicurus_knowledge.settings import KnowledgeSettings


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
    )
    docs_indexer = KnowledgeIndexer(
        doc_index,
        qdrant,
        platform,
        vault_path=settings.docs_path,
        tenant=settings.default_tenant_id,
        collection_base="docs",
        chunk_max_chars=settings.chunk_max_chars,
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
    module = build_module(vault_indexer, docs_indexer, module_docs_indexer)
    mcp_app = module.http_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with module.mcp.session_manager.run():
            await note_index.init()
            await doc_index.init()
            await module_doc_ledger.init()
            await bus.connect()
            log.info(
                "knowledge service ready",
                vault=str(settings.vault_path),
                docs=str(settings.docs_path),
                tenant=settings.default_tenant_id,
            )
            try:
                await vault_indexer.run()
            except Exception as exc:
                log.warning("initial vault index failed", error=str(exc))
            try:
                await docs_indexer.run()
            except Exception as exc:
                log.warning("initial docs index failed", error=str(exc))
            try:
                await module_docs_indexer.run()
            except Exception as exc:
                log.warning("initial module docs index failed", error=str(exc))
            try:
                yield
            finally:
                await bus.close()
                await engine.dispose()
                await qdrant.close()

    app = FastAPI(title=MODULE_NAME, lifespan=lifespan)
    add_ops_routes(app, service_name=MODULE_NAME, version=_service_version())
    add_manifest_route(app, module)

    # The editor page (#130): the shell renders it; this module supplies vault docs.
    app.include_router(create_pages_router(VaultPages(settings.vault_path, vault_indexer)))

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
        """Index statistics for the manifest-driven UI status panel."""
        note_count = await note_index.count(tenant=settings.default_tenant_id)
        last_indexed_at = await note_index.last_indexed_at(tenant=settings.default_tenant_id)
        doc_count = await doc_index.count(tenant=settings.default_tenant_id)
        module_doc_count = await module_doc_ledger.count(tenant=settings.default_tenant_id)
        return {
            "note_count": note_count,
            "last_indexed_at": last_indexed_at,
            "doc_count": doc_count,
            "module_doc_count": module_doc_count,
        }

    app.mount("/mcp", mcp_app)

    return app


app = create_app()
