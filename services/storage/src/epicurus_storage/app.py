"""Runnable storage service: ops endpoints + MCP tools + upload ingest + object surface.

After the file-space migration (ADR-0063) the core owns the file index and serves the Files
browser UI; storage owns the object store. So this app no longer mounts ``/data`` or scans a
tree — it exposes the object surface the core's Files view proxies (``/objects``, object read /
download / move) plus the chat-upload sink, and its MCP tools read the file space through the
core's platform API.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_core import (
    EventBus,
    PlatformClient,
    add_manifest_route,
    add_ops_routes,
    configure_logging,
    get_logger,
)
from epicurus_storage.db import FileIndex
from epicurus_storage.object_store import ObjectStore
from epicurus_storage.service import (
    MODULE_NAME,
    build_module,
    ingest_object,
    load_object_download,
    move_item,
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


class MoveBody(BaseModel):
    """Request body for ``POST /objects/move`` (shared move contract, #391)."""

    from_path: str
    to_path: str


class ObjectEntryOut(BaseModel):
    """One object-store entry, in the shape the core's Files view consumes."""

    path: str
    name: str
    size: int
    mtime: float
    kind: str


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
    # The agent file tools read the core-owned file space through the platform API (ADR-0063).
    platform = PlatformClient(
        base_url=settings.platform_url,
        tenant_id=settings.default_tenant_id,
        module=MODULE_NAME,
    )
    _tenant = settings.default_tenant_id
    module = build_module(
        index,
        objects,
        platform=platform,
        tenant=_tenant,
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
            log.info("storage service ready", tenant=_tenant)
            try:
                yield
            finally:
                await bus.close()
                await engine.dispose()

    app = FastAPI(title=MODULE_NAME, lifespan=lifespan)
    add_ops_routes(app, service_name=MODULE_NAME, version=_service_version())
    add_manifest_route(app, module)
    app.mount("/mcp", mcp_app)

    @app.post("/ingest")
    async def ingest(
        request: Request,
        filename: str = Query(..., description="Original filename of the uploaded file"),
        att_id: str = Query(default="", description="Core attachment id (uniqueness token)"),
    ) -> dict[str, object]:
        """Durably persist an uploaded file's bytes — the chat upload sink (ADR-0025).

        The core's attachment-upload route POSTs the raw bytes here (the ``Content-Type``
        header carries the media type) so the upload is kept in the object store and
        becomes browsable in the core Files page. Returns the stored object's
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

    # ── Object surface (the core's Files view proxies these, ADR-0063) ────────

    @app.get("/objects")
    async def list_objects(
        path: str = Query(default="", description="Directory to list (empty = root)"),
        q: str = Query(default="", description="Search query; if set, overrides path browsing"),
        tenant_id: str | None = Query(default=None),
    ) -> dict[str, list[ObjectEntryOut]]:
        """List/search object-store entries for the core's unified Files view.

        Returns ``{entries:[{path,name,size,mtime,kind}]}`` — browse under *path* when *q* is
        empty, otherwise search. The store is single-tenant (this module's default tenant); the
        ``tenant_id`` query is accepted for forward-compatibility.
        """
        if q.strip():
            entries = await index.search(tenant=_tenant, query=q.strip(), limit=200)
        else:
            entries = await index.browse(tenant=_tenant, path=path)
        return {
            "entries": [
                ObjectEntryOut(path=e.path, name=e.name, size=e.size, mtime=e.mtime, kind=e.kind)
                for e in entries
            ]
        }

    @app.get("/objects/read")
    async def read_object(
        path: str = Query(..., description="Object path to read"),
        tenant_id: str | None = Query(default=None),
    ) -> dict[str, object]:
        """Return an object's text for the Files split-screen reader. 404/413/415 on error."""
        obj = await load_object_download(index=index, objects=objects, tenant=_tenant, path=path)
        if obj is None:
            raise HTTPException(status_code=404, detail="not found")
        if len(obj.data) > READ_MAX_BYTES:
            raise HTTPException(status_code=413, detail="file is too large to preview")
        try:
            text = obj.data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=415, detail="file is not UTF-8 text") from exc
        return {"path": path, "name": obj.name, "content": text}

    @app.get("/download")
    async def download(
        path: str = Query(..., description="Object path to download"),
        tenant_id: str | None = Query(default=None),
    ) -> Response:
        """Stream a catalogued object from MinIO (the core proxies file-space files itself)."""
        obj = await load_object_download(index=index, objects=objects, tenant=_tenant, path=path)
        if obj is None:
            raise HTTPException(status_code=404, detail="not found")
        return Response(
            content=obj.data,
            media_type=obj.content_type,
            headers={"content-disposition": _attachment_disposition(obj.name)},
        )

    @app.post("/objects/move")
    async def move_object(
        body: MoveBody, tenant_id: str | None = Query(default=None)
    ) -> dict[str, str]:
        """Move/rename a writable object-store entry (#381 / #391); the core proxies this."""
        return await move_item(
            index=index,
            objects=objects,
            tenant=_tenant,
            from_path=body.from_path,
            to_path=body.to_path,
        )

    return app


app = create_app()
