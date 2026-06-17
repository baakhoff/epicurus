"""Incremental markdown-source indexer — walk a directory and sync Qdrant.

Used for both the operator's Obsidian vault and the bundled platform docs
(self-documentation, #83).  Only files that are new, modified (by content
hash), or deleted since the last run are touched.  Embeddings are obtained via
the core's platform API so the module never holds provider credentials (ADR-0010).
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from typing import TypedDict

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

from epicurus_core import PlatformClient, get_logger, scope_collection
from epicurus_knowledge.chunker import Chunk, chunk_note
from epicurus_knowledge.db import DocIndex, NoteIndex


class SearchHit(TypedDict):
    """One chunk returned by a semantic search query."""

    note_path: str
    heading: str | None
    text: str
    score: float


log = get_logger("knowledge.indexer")

# Fixed UUID5 namespace so chunk point IDs are deterministic across runs.
_CHUNK_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # standard DNS namespace


def _chunk_point_id(note_path: str, chunk_index: int) -> str:
    """Deterministic UUID5 point ID for a specific chunk within a note."""
    return str(uuid.uuid5(_CHUNK_NS, f"{note_path}:{chunk_index}"))


def _content_hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class KnowledgeIndexer:
    """Walks a markdown directory and maintains a Qdrant collection incrementally.

    Works for both the operator vault and the bundled platform docs — the
    caller controls which DB index and Qdrant collection to use via
    ``note_index`` and ``collection_base``.

    Args:
        note_index: Postgres-backed file hash/mtime tracker (NoteIndex or DocIndex).
        qdrant: Async Qdrant client.
        platform: Platform API client (embeddings come from the core).
        vault_path: Root directory to walk for ``.md`` files.
        tenant: Tenant ID — scopes the Qdrant collection name.
        collection_base: Base name passed to ``scope_collection``; becomes
            ``<tenant>__<base>`` in Qdrant.  Defaults to ``"knowledge"`` for
            the vault; use ``"docs"`` for the platform-docs source.
        chunk_max_chars: Upper-bound on characters per chunk.
    """

    def __init__(
        self,
        note_index: NoteIndex | DocIndex,
        qdrant: AsyncQdrantClient,
        platform: PlatformClient,
        *,
        vault_path: Path,
        tenant: str,
        collection_base: str = "knowledge",
        chunk_max_chars: int = 2000,
    ) -> None:
        self._notes = note_index
        self._qdrant = qdrant
        self._platform = platform
        self._vault = vault_path
        self._tenant = tenant
        self._max_chars = chunk_max_chars
        self._collection = scope_collection(collection_base, tenant)
        self._ensured = False

    async def _ensure_collection(self, dim: int) -> None:
        if self._ensured:
            return
        if not await self._qdrant.collection_exists(self._collection):
            await self._qdrant.create_collection(
                self._collection,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )
        self._ensured = True

    async def _delete_note_vectors(self, note_path: str) -> None:
        """Remove all Qdrant points whose payload ``note_path`` matches."""
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

    async def _index_note(self, note_path: str, content: str) -> int:
        """Chunk, embed, and upsert one note.  Returns the number of chunks indexed."""
        chunks: list[Chunk] = chunk_note(content, self._max_chars)
        if not chunks:
            return 0

        texts = [c.text for c in chunks]
        vectors = await self._platform.embed(texts)
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

    async def search(self, query: str, k: int = 5) -> list[SearchHit]:
        """Return the top-*k* chunks most semantically similar to *query*.

        Embeds *query* via the core's LLM gateway, then queries the tenant's
        Qdrant collection.  Returns an empty list if the collection has not been
        created yet (i.e. no notes have been indexed).

        Args:
            query: Natural-language question or search phrase.
            k: Maximum number of chunks to return.

        Returns a list of :class:`SearchHit` dicts ordered by descending score.
        """
        if not await self._qdrant.collection_exists(self._collection):
            return []
        [query_vec] = await self._platform.embed([query])
        # qdrant-client 1.14 removed the legacy `search`; `query_points` is the
        # current API (mirrors core-app's memory recall). Results are on `.points`.
        response = await self._qdrant.query_points(
            collection_name=self._collection,
            query=query_vec,
            limit=k,
            with_payload=True,
        )
        results: list[SearchHit] = []
        for hit in response.points:
            if not hit.payload:
                continue
            results.append(
                SearchHit(
                    note_path=str(hit.payload.get("note_path", "")),
                    heading=str(hit.payload["heading"]) if hit.payload.get("heading") else None,
                    text=str(hit.payload.get("text", "")),
                    score=float(hit.score),
                )
            )
        return results

    async def index_path(self, rel: str) -> int:
        """Re-index a single file by its vault-relative path; returns the chunk count.

        The editor save path (#130) writes a document and then re-embeds just that
        file rather than walking the whole vault. Any prior vectors for the path are
        deleted first so a shrunk document leaves no stale chunks behind. The DB
        ledger is updated so the next full ``run`` treats the file as unchanged.
        """
        fpath = self._vault / rel
        raw = fpath.read_bytes()
        content_hash = _content_hash(raw)
        if await self._notes.get(tenant=self._tenant, note_path=rel) is not None:
            await self._delete_note_vectors(rel)
        content = raw.decode("utf-8", errors="replace")
        chunk_count = await self._index_note(rel, content)
        await self._notes.upsert(
            tenant=self._tenant,
            note_path=rel,
            mtime_ns=fpath.stat().st_mtime_ns,
            content_hash=content_hash,
            chunk_count=chunk_count,
        )
        log.debug("re-indexed single note", path=rel, chunks=chunk_count)
        return chunk_count

    async def run(self) -> dict[str, int]:
        """Walk the vault and incrementally update the Qdrant index.

        Returns::

            {"indexed": N, "deleted": M, "unchanged": K}

        where *N* notes were re-indexed, *M* were removed, and *K* were skipped
        because their content hash was unchanged.
        """
        indexed = 0
        unchanged = 0

        seen_paths: set[str] = set()

        if not self._vault.exists():
            log.warning("vault path does not exist", path=str(self._vault))
            return {"indexed": 0, "deleted": 0, "unchanged": 0}

        for dirpath, _dirs, filenames in os.walk(self._vault):
            dir_abs = Path(dirpath)
            for fname in filenames:
                if not fname.endswith(".md"):
                    continue
                fpath = dir_abs / fname
                try:
                    st = fpath.stat()
                except OSError:
                    continue
                rel = fpath.relative_to(self._vault).as_posix()
                seen_paths.add(rel)

                mtime_ns = st.st_mtime_ns
                existing = await self._notes.get(tenant=self._tenant, note_path=rel)

                if existing is not None and existing.mtime_ns == mtime_ns:
                    # Fast path: mtime unchanged, skip reading the file.
                    unchanged += 1
                    continue

                raw = fpath.read_bytes()
                content_hash = _content_hash(raw)

                if existing is not None and existing.content_hash == content_hash:
                    # File was touched but content is identical; update mtime only.
                    await self._notes.upsert(
                        tenant=self._tenant,
                        note_path=rel,
                        mtime_ns=mtime_ns,
                        content_hash=content_hash,
                        chunk_count=existing.chunk_count,
                    )
                    unchanged += 1
                    continue

                # New or genuinely changed note — re-index.
                if existing is not None:
                    await self._delete_note_vectors(rel)

                content = raw.decode("utf-8", errors="replace")
                chunk_count = await self._index_note(rel, content)
                await self._notes.upsert(
                    tenant=self._tenant,
                    note_path=rel,
                    mtime_ns=mtime_ns,
                    content_hash=content_hash,
                    chunk_count=chunk_count,
                )
                indexed += 1
                log.debug("indexed note", path=rel, chunks=chunk_count)

        # Delete notes that were removed from the vault since the last run.
        known_paths = set(await self._notes.list_paths(tenant=self._tenant))
        stale_paths = known_paths - seen_paths
        for stale in stale_paths:
            await self._delete_note_vectors(stale)
            await self._notes.delete(tenant=self._tenant, note_path=stale)
        deleted = len(stale_paths)

        log.info(
            "vault index run complete",
            tenant=self._tenant,
            indexed=indexed,
            deleted=deleted,
            unchanged=unchanged,
        )
        return {"indexed": indexed, "deleted": deleted, "unchanged": unchanged}
