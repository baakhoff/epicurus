"""Runnable storage service: ops endpoints + MCP tools + upload ingest + download."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
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
from epicurus_storage.service import (
    MODULE_NAME,
    STORAGE_PAGE_ID,
    build_module,
    build_page_data,
    ingest_object,
    load_object_download,
    load_text_file,
)
from epicurus_storage.settings import READ_MAX_BYTES, StorageSettings


def _attachment_disposition(name: str) -> str:
    """A ``Content-Disposition`` header that downloads as *name* (header-safe)."""
    safe = name.replace('"', "").replace("\\", "").replace("\r", "").replace("\n", "")
    return f'attachment; filename="{safe}"'


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
    # Tenant-scope the served/indexed tree (constraint #1): /data/<tenant>. The whole
    # epicurus-files volume mounts at /data (storage gets it read-only); we serve, index,
    # and confine reads/downloads to the tenant subtree so knowledge (/data/<tenant>/knowledge)
    # and notes (/data/<tenant>/notes) show up, while sibling tenants stay invisible. The
    # relative_to() confinement on /read + /download then scopes to this subtree automatically.
    served_root = settings.storage_root / settings.default_tenant_id
    module = build_module(
        index,
        objects,
        storage_root=str(served_root),
        tenant=settings.default_tenant_id,
        hidden_prefixes=tuple(
            p.strip() for p in settings.agent_hidden_prefixes.split(",") if p.strip()
        ),
    )
    mcp_app = module.http_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with module.mcp.session_manager.run():
            await index.init()
            await bus.connect()
            log.info(
                "storage service ready",
                root=str(served_root),
                tenant=settings.default_tenant_id,
            )
            try:
                await scan(served_root, index, tenant=settings.default_tenant_id)
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

    _root = served_root
    _tenant = settings.default_tenant_id
    _download_base = f"/platform/v1/modules/{MODULE_NAME}/download"

    @app.post("/ingest")
    async def ingest(
        request: Request,
        filename: str = Query(..., description="Original filename of the uploaded file"),
        att_id: str = Query(default="", description="Core attachment id (uniqueness token)"),
    ) -> dict[str, object]:
        """Durably persist an uploaded file's bytes — the chat upload sink (ADR-0025).

        The core's attachment-upload route POSTs the raw bytes here (the ``Content-Type``
        header carries the media type) so the upload is kept in the object store and
        becomes browsable in the Files page. Returns the stored object's
        ``{key, name, size}``.
        """
        data = await request.body()
        content_type = request.headers.get("content-type") or "application/octet-stream"
        return await ingest_object(
            index=index,
            objects=objects,
            tenant=_tenant,
            att_id=att_id,
            filename=filename,
            content_type=content_type,
            data=data,
        )

    @app.get("/download")
    async def download(
        path: str = Query(..., description="Path relative to the storage root"),
    ) -> Response:
        """Stream a file — a catalogued object or a file from the indexed tree.

        Any ``source="object"`` entry (a chat upload or a file the agent saved) is streamed
        from MinIO; every other *path* is resolved against the configured storage root, with
        path-traversal attempts (``..`` components) rejected with HTTP 400.
        """
        # The index entry's source is the sole authority for object-vs-filesystem: an object
        # (a chat upload OR a file the agent saved under any key) streams from MinIO, everything
        # else resolves against the read-only tree. We can't shortcut by an `uploads/` prefix —
        # agent objects aren't confined to it (#347) — so we always consult the catalogue first;
        # `load_object_download` returns None for a filesystem entry before touching MinIO.
        try:
            obj = await load_object_download(
                index=index, objects=objects, tenant=_tenant, path=path
            )
        except Exception as exc:  # an index/object hiccup must not break fs downloads
            log.warning("object download lookup failed", path=path, error=str(exc))
            obj = None
        if obj is not None:
            return Response(
                content=obj.data,
                media_type=obj.content_type,
                headers={"content-disposition": _attachment_disposition(obj.name)},
            )

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

    @app.get("/read")
    async def read_text(
        path: str = Query(..., description="Path relative to the storage root"),
    ) -> dict[str, object]:
        """Return a text file's contents for the Files split-screen reader (#KB-refactor).

        A catalogued ``source="object"`` entry (upload or agent-written) is decoded from MinIO;
        every other *path* is read from the read-only tree. 400 traversal, 404 missing, 413 too
        large, 415 binary.
        """
        # Same source-led routing as /download: an object (upload or agent-written) decodes
        # from MinIO, anything else reads from the read-only tree (#347).
        try:
            obj = await load_object_download(
                index=index, objects=objects, tenant=_tenant, path=path
            )
        except Exception as exc:  # an index/object hiccup must not break fs reads
            log.warning("object read lookup failed", path=path, error=str(exc))
            obj = None
        if obj is not None:
            if len(obj.data) > READ_MAX_BYTES:
                raise HTTPException(status_code=413, detail="file is too large to preview")
            try:
                text = obj.data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise HTTPException(status_code=415, detail="file is not UTF-8 text") from exc
            return {"path": path, "name": obj.name, "content": text}
        return load_text_file(_root, path).model_dump()

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
