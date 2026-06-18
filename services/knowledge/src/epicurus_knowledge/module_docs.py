"""Per-module documentation indexing (#215).

At startup and on re-index, fetches documentation from each enabled module that
declares a ``docs_url`` in its manifest.  Documents are written into the
``<tenant>__docs`` Qdrant collection alongside the bundled platform docs, using a
``module/<name>/`` path prefix so they can be diffed and purged independently when
a module is disabled or removed.

The response the knowledge module expects from each module's docs endpoint is::

    {"documents": [{"path": "usage.md", "content": "# Usage\\n..."}, ...]}

The core proxies the module's ``docs_url`` at ``GET /platform/v1/modules/{name}/docs``
so the knowledge service never calls a module directly.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchValue,
    PointStruct,
    VectorParams,
)
from sqlalchemy import DateTime, Integer, String, UniqueConstraint, delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from epicurus_core import PlatformClient, get_logger
from epicurus_core.tenancy import scope_collection
from epicurus_knowledge.chunker import chunk_note

_log = get_logger("knowledge.module_docs")

# Separate UUID namespace from the main indexer so point ids never collide.
_CHUNK_NS = uuid.UUID("b4a5c6d7-e8f9-4a5b-8c9d-0e1f2a3b4c5d")


def _chunk_point_id(note_path: str, chunk_index: int) -> str:
    return str(uuid.uuid5(_CHUNK_NS, f"{note_path}:{chunk_index}"))


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _module_path(module_name: str, doc_path: str) -> str:
    """Namespace a module doc path so it never collides with platform docs."""
    return f"module/{module_name}/{doc_path}"


# ── DB ledger ─────────────────────────────────────────────────────────────────


class _ModuleDocBase(DeclarativeBase):
    pass


class _StoredModuleDoc(_ModuleDocBase):
    """One indexed per-module doc page, tracked for incremental re-indexing."""

    __tablename__ = "knowledge_module_docs"
    __table_args__ = (UniqueConstraint("tenant", "module_name", "doc_path", name="uq_module_doc"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    module_name: Mapped[str] = mapped_column(String(63))
    doc_path: Mapped[str] = mapped_column(String(4096))
    content_hash: Mapped[str] = mapped_column(String(64))
    chunk_count: Mapped[int] = mapped_column(Integer)
    indexed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ModuleDocLedger:
    """Postgres-backed tracker for per-module documentation pages."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def init(self) -> None:
        """Create the schema if it does not exist."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_ModuleDocBase.metadata.create_all)

    async def get_hash(self, *, tenant: str, module_name: str, doc_path: str) -> str | None:
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredModuleDoc).where(
                    _StoredModuleDoc.tenant == tenant,
                    _StoredModuleDoc.module_name == module_name,
                    _StoredModuleDoc.doc_path == doc_path,
                )
            )
            return row.content_hash if row is not None else None

    async def upsert(
        self,
        *,
        tenant: str,
        module_name: str,
        doc_path: str,
        content_hash: str,
        chunk_count: int,
    ) -> None:
        async with self._session() as session:
            await session.execute(
                delete(_StoredModuleDoc).where(
                    _StoredModuleDoc.tenant == tenant,
                    _StoredModuleDoc.module_name == module_name,
                    _StoredModuleDoc.doc_path == doc_path,
                )
            )
            session.add(
                _StoredModuleDoc(
                    tenant=tenant,
                    module_name=module_name,
                    doc_path=doc_path,
                    content_hash=content_hash,
                    chunk_count=chunk_count,
                )
            )
            await session.commit()

    async def list_paths(self, *, tenant: str, module_name: str) -> list[str]:
        async with self._session() as session:
            rows = await session.scalars(
                select(_StoredModuleDoc.doc_path).where(
                    _StoredModuleDoc.tenant == tenant,
                    _StoredModuleDoc.module_name == module_name,
                )
            )
            return list(rows)

    async def list_modules(self, *, tenant: str) -> list[str]:
        """Return the distinct module names that have indexed docs for *tenant*."""
        async with self._session() as session:
            rows = await session.scalars(
                select(_StoredModuleDoc.module_name)
                .where(_StoredModuleDoc.tenant == tenant)
                .distinct()
            )
            return list(rows)

    async def delete_doc(self, *, tenant: str, module_name: str, doc_path: str) -> None:
        async with self._session() as session:
            await session.execute(
                delete(_StoredModuleDoc).where(
                    _StoredModuleDoc.tenant == tenant,
                    _StoredModuleDoc.module_name == module_name,
                    _StoredModuleDoc.doc_path == doc_path,
                )
            )
            await session.commit()

    async def delete_module(self, *, tenant: str, module_name: str) -> None:
        """Remove all ledger rows for *module_name* (e.g. after it was disabled)."""
        async with self._session() as session:
            await session.execute(
                delete(_StoredModuleDoc).where(
                    _StoredModuleDoc.tenant == tenant,
                    _StoredModuleDoc.module_name == module_name,
                )
            )
            await session.commit()

    async def count(self, *, tenant: str) -> int:
        async with self._session() as session:
            result = await session.scalar(
                select(func.count(_StoredModuleDoc.id)).where(_StoredModuleDoc.tenant == tenant)
            )
            return int(result) if result is not None else 0

    async def clear(self, *, tenant: str) -> None:
        """Delete every module-doc row for *tenant* (self-heal after a Qdrant reset, #229)."""
        async with self._session() as session:
            await session.execute(delete(_StoredModuleDoc).where(_StoredModuleDoc.tenant == tenant))
            await session.commit()


# ── Indexer ───────────────────────────────────────────────────────────────────


class ModuleDocsIndexer:
    """Indexes per-module documentation into the ``<tenant>__docs`` Qdrant collection.

    Fetches docs from each enabled module that declares a ``docs_url`` in its
    manifest (via the core proxy), writes them under a ``module/<name>/`` path
    prefix alongside the bundled platform docs, and purges docs for modules that
    are disabled, removed, or no longer declare ``docs_url``.

    Documents are indexed incrementally: unchanged content (same SHA-256 hash) is
    skipped; changed content is re-embedded and upserted; paths no longer served
    are deleted.
    """

    def __init__(
        self,
        ledger: ModuleDocLedger,
        qdrant: AsyncQdrantClient,
        platform: PlatformClient,
        *,
        tenant: str,
        chunk_max_chars: int = 2000,
    ) -> None:
        self._ledger = ledger
        self._qdrant = qdrant
        self._platform = platform
        self._tenant = tenant
        self._max_chars = chunk_max_chars
        self._collection = scope_collection("docs", tenant)

    async def _embedding_model(self) -> str | None:
        """Resolve the knowledge module's chosen embedding model slot (#128)."""
        return await self._platform.get_module_model("embedding")

    async def _ensure_collection(self, dim: int) -> None:
        if not await self._qdrant.collection_exists(self._collection):
            await self._qdrant.create_collection(
                self._collection,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )

    async def _delete_note_vectors(self, note_path: str) -> None:
        if not await self._qdrant.collection_exists(self._collection):
            return
        await self._qdrant.delete(
            collection_name=self._collection,
            points_selector=FilterSelector(
                filter=Filter(
                    must=[FieldCondition(key="note_path", match=MatchValue(value=note_path))]
                )
            ),
        )

    async def _index_doc(self, note_path: str, content: str, *, model: str | None) -> int:
        """Chunk, embed, and upsert one document.  Returns the number of chunks written."""
        chunks = chunk_note(content, self._max_chars)
        if not chunks:
            return 0
        texts = [c.text for c in chunks]
        vectors = await self._platform.embed(texts, model=model)
        await self._ensure_collection(len(vectors[0]))
        points = [
            PointStruct(
                id=_chunk_point_id(note_path, c.index),
                vector=vectors[i],
                payload={
                    "note_path": note_path,
                    "chunk_index": c.index,
                    "heading": c.heading,
                    "text": c.text,
                },
            )
            for i, c in enumerate(chunks)
        ]
        await self._qdrant.upsert(collection_name=self._collection, points=points)
        return len(chunks)

    async def reconcile(self) -> bool:
        """Self-heal after a Qdrant reset (#229): clear the ledger if ``<tenant>__docs`` is gone.

        Shares the ``<tenant>__docs`` collection with the platform-docs indexer, so this
        must run **before** that indexer's ``run`` recreates the collection — otherwise the
        collection would already exist and the unchanged-hash check would skip re-indexing
        the module docs. The runner reconciles all sources up front to guarantee this order.
        Returns ``True`` when it cleared the ledger.
        """
        if await self._qdrant.collection_exists(self._collection):
            return False
        known = await self._ledger.count(tenant=self._tenant)
        if known == 0:
            return False
        _log.warning(
            "qdrant docs collection missing but module-doc ledger non-empty; clearing to re-index",
            collection=self._collection,
            ledger_rows=known,
        )
        await self._ledger.clear(tenant=self._tenant)
        return True

    async def run(self) -> dict[str, int]:
        """Sync module docs: index new/changed, purge disabled/removed modules.

        Returns ``{"indexed": N, "deleted": M, "unchanged": K}``.

        The run is best-effort per module: a fetch failure for one module is logged
        and skipped; other modules continue.
        """
        indexed = deleted = unchanged = 0

        # Collect enabled modules that declare docs_url.
        try:
            snapshots = await self._platform.list_modules()
        except Exception as exc:
            _log.warning("module_docs: could not list modules", error=str(exc))
            return {"indexed": 0, "deleted": 0, "unchanged": 0}

        active: dict[str, list[dict[str, Any]]] = {}
        for snap in snapshots:
            if not snap.get("enabled", True) or snap.get("removed", False):
                continue
            docs_url = (snap.get("manifest") or {}).get("docs_url")
            if not docs_url:
                continue
            name = (snap.get("manifest") or {}).get("name", "")
            if not name:
                continue
            try:
                docs = await self._platform.get_module_docs(name)
            except Exception as exc:
                _log.warning("module_docs: could not fetch docs", module=name, error=str(exc))
                continue
            active[name] = docs

        model = await self._embedding_model()

        # Sync docs for currently-active modules.
        for name, docs in active.items():
            fetched_paths = {d["path"] for d in docs}
            indexed_paths = set(
                await self._ledger.list_paths(tenant=self._tenant, module_name=name)
            )

            # Remove docs the module no longer serves.
            for removed_path in indexed_paths - fetched_paths:
                note_path = _module_path(name, removed_path)
                await self._delete_note_vectors(note_path)
                await self._ledger.delete_doc(
                    tenant=self._tenant, module_name=name, doc_path=removed_path
                )
                deleted += 1

            # Index new or changed docs.
            for doc in docs:
                doc_path: str = doc["path"]
                content: str = doc["content"]
                new_hash = _content_hash(content)
                old_hash = await self._ledger.get_hash(
                    tenant=self._tenant, module_name=name, doc_path=doc_path
                )
                if old_hash == new_hash:
                    unchanged += 1
                    continue
                note_path = _module_path(name, doc_path)
                await self._delete_note_vectors(note_path)
                chunk_count = await self._index_doc(note_path, content, model=model)
                await self._ledger.upsert(
                    tenant=self._tenant,
                    module_name=name,
                    doc_path=doc_path,
                    content_hash=new_hash,
                    chunk_count=chunk_count,
                )
                indexed += 1

        # Purge docs for modules no longer in the active set (disabled / removed / no docs_url).
        ledger_modules = set(await self._ledger.list_modules(tenant=self._tenant))
        for stale_name in ledger_modules - set(active):
            stale_paths = await self._ledger.list_paths(tenant=self._tenant, module_name=stale_name)
            for doc_path in stale_paths:
                note_path = _module_path(stale_name, doc_path)
                await self._delete_note_vectors(note_path)
                deleted += 1
            await self._ledger.delete_module(tenant=self._tenant, module_name=stale_name)

        _log.info(
            "module_docs indexed",
            indexed=indexed,
            deleted=deleted,
            unchanged=unchanged,
        )
        return {"indexed": indexed, "deleted": deleted, "unchanged": unchanged}

    async def doc_count(self) -> int:
        """Number of module doc pages indexed for this tenant."""
        return await self._ledger.count(tenant=self._tenant)
