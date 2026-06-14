"""Runnable storage service: ops endpoints + MCP tool surface + file download."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_core import (
    EventBus,
    add_manifest_route,
    add_ops_routes,
    configure_logging,
    get_logger,
)
from epicurus_storage.db import FileIndex
from epicurus_storage.object_store import ObjectStore
from epicurus_storage.scanner import scan
from epicurus_storage.service import MODULE_NAME, STORAGE_PAGE_ID, build_module, build_page_data
from epicurus_storage.settings import StorageSettings


def _service_version() -> str:
    try:
        return pkg_version("epicurus-storage")
    except PackageNotFoundError:
        return "0.0.0"


def create_app() -> FastAPI:
    """Build the storage ASGI app (connects at startup)."""
    settings = StorageSettings(service_name=MODULE_NAME)
    configure_logging(settings)
    log = get_logger(MODULE_NAME)

    engine = create_async_engine(settings.database_url)
    index = FileIndex(engine)
    objects = ObjectStore(
        url=settings.minio_url,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
    )
    bus = EventBus.from_settings(settings)
    module = build_module(
        index,
        objects,
        storage_root=str(settings.storage_root),
        tenant=settings.default_tenant_id,
    )
    mcp_app = module.http_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with module.mcp.session_manager.run():
            await index.init()
            await bus.connect()
            log.info(
                "storage service ready",
                root=str(settings.storage_root),
                tenant=settings.default_tenant_id,
            )
            try:
                await scan(settings.storage_root, index, tenant=settings.default_tenant_id)
            except Exception as exc:
                log.warning("initial scan failed", error=str(exc))
            try:
                yield
            finally:
                await bus.close()
                await engine.dispose()

    app = FastAPI(title=MODULE_NAME, lifespan=lifespan)
    add_ops_routes(app, service_name=MODULE_NAME, version=_service_version())
    add_manifest_route(app, module)
    app.mount("/mcp", mcp_app)

    _root = settings.storage_root
    _tenant = settings.default_tenant_id
    _download_base = f"/platform/v1/modules/{MODULE_NAME}/download"

    @app.get("/download")
    async def download(
        path: str = Query(..., description="Path relative to the storage root"),
    ) -> FileResponse:
        """Stream a file from the indexed tree.

        *path* must be relative to the configured storage root; path-traversal
        attempts (``..`` components) are rejected with HTTP 400.
        """
        try:
            resolved = (_root / path).resolve()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="invalid path") from exc

        try:
            resolved.relative_to(_root.resolve())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="path escapes storage root") from exc

        if not resolved.exists():
            raise HTTPException(status_code=404, detail="not found")
        if not resolved.is_file():
            raise HTTPException(status_code=400, detail="path is not a file")

        return FileResponse(str(resolved), filename=resolved.name)

    @app.get("/pages/{page_id}")
    async def page(
        page_id: str,
        path: str = Query(default="", description="Directory path to browse (empty = root)"),
        q: str = Query(default="", description="Search query; if set, overrides path browsing"),
    ) -> dict[str, object]:
        """Serve the Files browser page data (ADR-0018); the core proxies this.

        Returns a ``BrowserData``-shaped payload: ``{title, path, search_enabled, items}``.
        Each item carries ``nav_path`` (for directories) or ``href`` (for files) so the
        shell can navigate and download without talking to the module directly.
        """
        if page_id != STORAGE_PAGE_ID:
            raise HTTPException(status_code=404, detail=f"no page {page_id!r}")

        if q.strip():
            entries = await index.search(tenant=_tenant, query=q.strip(), limit=200)
        else:
            entries = await index.browse(tenant=_tenant, path=path)

        return build_page_data(entries, path=path, query=q.strip(), download_base=_download_base)

    return app


app = create_app()
