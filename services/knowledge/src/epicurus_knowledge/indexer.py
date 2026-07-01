"""Incremental markdown-source indexer — walk a directory and sync Qdrant.

Used for both the operator's Obsidian vault and the bundled platform docs
(self-documentation, #83).  Only files that are new, modified (by content
hash), or deleted since the last run are touched.  Embeddings are obtained via
the core's platform API so the module never holds provider credentials (ADR-0010).
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass
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
from epicurus_knowledge.reader import DiskVaultReader, VaultReader


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


def _mtime_ns(mtime: float) -> int:
    """A :class:`~epicurus_core.files.FileEntry` float ``mtime`` (seconds) as integer ns.

    The ledger tracks ``mtime_ns`` for the fast "unchanged" skip. Reads now come from the
    file API, which reports ``mtime`` as float seconds (``os.stat_result.st_mtime``, or S3
    ``LastModified``), so the ns value is derived, not the raw ``st_mtime_ns``. It is stable
    run-to-run for an unchanged file, so the skip still holds; the first pass after this
    change re-reads each file once (its stored raw-ns won't match the derived value) but the
    content hash matches, so nothing re-embeds — a one-time reconcile, no data churn.
    """
    return round(mtime * 1_000_000_000)


@dataclass(slots=True)
class _PendingNote:
    """A new/changed note awaiting batched embedding during a ``run`` (#230)."""

    rel: str
    mtime_ns: int
    content_hash: str
    chunks: list[Chunk]


class KnowledgeIndexer:
    """Walks a markdown directory and maintains a Qdrant collection incrementally.

    Works for both the operator vault and the bundled platform docs — the
    caller controls which DB index and Qdrant collection to use via
    ``note_index`` and ``collection_base``.

    Args:
        note_index: Postgres-backed file hash/mtime tracker (NoteIndex or DocIndex).
        qdrant: Async Qdrant client.
        platform: Platform API client (embeddings come from the core).
        vault_path: Root directory to read ``.md`` files from. A convenience: when no
            ``reader`` is given it becomes a :class:`~reader.DiskVaultReader` over this path
            (the bundled docs source, the watch-mode vault, and the tests).
        reader: The vault read backend (#346, ADR-0064). The default (normal mode) is an
            :class:`~reader.ApiVaultReader` so the module reads through the core file API and
            mounts no ``/data`` volume; watch mode and the docs source pass a disk reader.
            Exactly one of ``reader`` / ``vault_path`` supplies the read root.
        tenant: Tenant ID — scopes the Qdrant collection name.
        collection_base: Base name passed to ``scope_collection``; becomes
            ``<tenant>__<base>`` in Qdrant.  Defaults to ``"knowledge"`` for
            the vault; use ``"docs"`` for the platform-docs source.
        chunk_max_chars: Upper-bound on characters per chunk.
        embed_batch_size: How many chunk texts to embed per platform-API call.
            ``run`` accumulates chunks across files and flushes a batch once this
            many are pending, so the bundled docs index in a handful of round-trips
            instead of one per file (#230).
    """

    def __init__(
        self,
        note_index: NoteIndex | DocIndex,
        qdrant: AsyncQdrantClient,
        platform: PlatformClient,
        *,
        vault_path: Path | None = None,
        reader: VaultReader | None = None,
        tenant: str,
        collection_base: str = "knowledge",
        chunk_max_chars: int = 2000,
        embed_batch_size: int = 64,
    ) -> None:
        if reader is None:
            if vault_path is None:
                raise ValueError("KnowledgeIndexer needs a reader or a vault_path")
            reader = DiskVaultReader(vault_path)
        self._notes = note_index
        self._qdrant = qdrant
        self._platform = platform
        self._reader = reader
        self._tenant = tenant
        self._max_chars = chunk_max_chars
        self._batch_size = max(1, embed_batch_size)
        self._collection = scope_collection(collection_base, tenant)
        self._ensured = False
        # Serialises full re-index passes on this indexer instance. The vault indexer is
        # shared between the startup runner (#230) and the live watcher (#232), and the
        # Re-index action can fire mid-startup; without this two concurrent walks could
        # double-embed or race the ledger. Held only by run(); single-file index_path /
        # remove_path stay lock-free (in watch mode the vault is read-only, so they and a
        # watch pass never overlap).
        self._run_lock = asyncio.Lock()

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

    async def _embedding_model(self) -> str | None:
        """The operator's chosen embedding model for the knowledge module (#128).

        ``None`` means no selection — :meth:`PlatformClient.embed` then falls back to the
        core's default embedding model.
        """
        return await self._platform.get_module_model("embedding")

    async def _index_note(self, note_path: str, content: str, *, model: str | None) -> int:
        """Chunk, embed, and upsert one note.  Returns the number of chunks indexed.

        ``model`` is the operator's chosen embedding model (resolve it once per run via
        :meth:`_embedding_model` and thread it in), or ``None`` for the core default.
        """
        chunks: list[Chunk] = chunk_note(content, self._max_chars)
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

    async def _flush_batch(self, pending: list[_PendingNote], *, model: str | None) -> None:
        """Embed every pending note's chunks in one platform call, then upsert + record.

        Batches the embedding across files (#230): one ``/embed`` round-trip covers
        all chunks in *pending*, after which each note's vectors are upserted and its
        ledger row written. The ledger is updated only after a note's vectors land, so
        an interrupted run leaves the ledger consistent (the note re-indexes next time).
        """
        if not pending:
            return
        texts = [c.text for note in pending for c in note.chunks]
        vectors = await self._platform.embed(texts, model=model)
        await self._ensure_collection(len(vectors[0]))
        offset = 0
        for note in pending:
            points = [
                PointStruct(
                    id=_chunk_point_id(note.rel, c.index),
                    vector=vectors[offset + i],
                    payload={
                        "note_path": note.rel,
                        "chunk_index": c.index,
                        "heading": c.heading,
                        "text": c.text,
                    },
                )
                for i, c in enumerate(note.chunks)
            ]
            offset += len(note.chunks)
            await self._qdrant.upsert(collection_name=self._collection, points=points)
            await self._notes.upsert(
                tenant=self._tenant,
                note_path=note.rel,
                mtime_ns=note.mtime_ns,
                content_hash=note.content_hash,
                chunk_count=len(note.chunks),
            )

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
        model = await self._embedding_model()
        [query_vec] = await self._platform.embed([query], model=model)
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

    async def remove_path(self, rel: str) -> None:
        """De-index a single file by its vault-relative path (the file was deleted).

        Drops the file's Qdrant vectors and its ledger row so a deleted document
        stops surfacing in search immediately, rather than lingering until the next
        full ``run`` reconciles the filesystem. Idempotent: removing an unknown path
        is a no-op. Used when approving a delete suggestion (#220).
        """
        await self._delete_note_vectors(rel)
        await self._notes.delete(tenant=self._tenant, note_path=rel)
        log.debug("de-indexed single note", path=rel)

    async def remove_under(self, prefix: str) -> int:
        """De-index every note whose path is under *prefix* (e.g. a deleted folder/project).

        Drops each matching note's Qdrant vectors and ledger row so a removed knowledge base
        stops surfacing in search immediately, rather than lingering until the next full
        ``run`` reconciles the filesystem. *prefix* should end with ``"/"`` to match a
        directory boundary. Returns the number of notes removed. Idempotent: an unknown
        prefix removes nothing. Used when deleting a knowledge base (#340).
        """
        paths = [
            p for p in await self._notes.list_paths(tenant=self._tenant) if p.startswith(prefix)
        ]
        for rel in paths:
            await self._delete_note_vectors(rel)
            await self._notes.delete(tenant=self._tenant, note_path=rel)
        if paths:
            log.info("de-indexed notes under prefix", prefix=prefix, count=len(paths))
        return len(paths)

    async def index_path(self, rel: str) -> int:
        """Re-index a single file by its vault-relative path; returns the chunk count.

        The editor save path (#130) writes a document and then re-embeds just that
        file rather than walking the whole vault. Any prior vectors for the path are
        deleted first so a shrunk document leaves no stale chunks behind. The DB
        ledger is updated so the next full ``run`` treats the file as unchanged.

        The content is read back through the file API (the core wrote it, ADR-0064); a
        vanished file (``None``) raises so the caller's best-effort ``indexed=False`` path
        fires rather than a silent no-op.
        """
        content = await self._reader.read_text(rel)
        if content is None:
            raise FileNotFoundError(rel)
        content_hash = _content_hash(content.encode("utf-8"))
        if await self._notes.get(tenant=self._tenant, note_path=rel) is not None:
            await self._delete_note_vectors(rel)
        chunk_count = await self._index_note(rel, content, model=await self._embedding_model())
        entry = await self._reader.stat(rel)
        await self._notes.upsert(
            tenant=self._tenant,
            note_path=rel,
            mtime_ns=_mtime_ns(entry.mtime) if entry is not None else 0,
            content_hash=content_hash,
            chunk_count=chunk_count,
        )
        log.debug("re-indexed single note", path=rel, chunks=chunk_count)
        return chunk_count

    async def reconcile(self) -> bool:
        """Self-heal after a Qdrant reset (#229): drop a stale ledger so ``run`` re-indexes.

        qdrant vectors are derived data and may be wiped on a server upgrade (see the
        ``qdrant-init`` guard). If our collection is gone but the Postgres ledger still
        lists files as indexed, the incremental walk would skip every file and leave the
        collection empty. Detect that drift and clear the ledger so the next ``run``
        re-embeds from scratch. Returns ``True`` when it cleared the ledger.

        Must run for *all* sources before any ``run`` recreates a collection — the vault
        and module-docs share ``<tenant>__docs`` with the platform docs, so the runner
        reconciles every source up front (see :class:`runner.IndexRunner`).
        """
        if await self._qdrant.collection_exists(self._collection):
            return False
        known = await self._notes.count(tenant=self._tenant)
        if known == 0:
            return False
        log.warning(
            "qdrant collection missing but ledger non-empty; clearing ledger to re-index",
            collection=self._collection,
            ledger_rows=known,
        )
        await self._notes.clear(tenant=self._tenant)
        self._ensured = False
        return True

    async def reset(self) -> None:
        """Drop this source's vectors **and** ledger so the next ``run`` re-embeds from scratch.

        The re-embed action (#332) calls this when the embedding model changes: vectors made
        with the old model are incompatible, and the incremental ledger would otherwise skip
        every "unchanged" file. Held under the run-lock so it can't race an in-flight ``run``.
        """
        async with self._run_lock:
            if await self._qdrant.collection_exists(self._collection):
                await self._qdrant.delete_collection(self._collection)
            await self._notes.clear(tenant=self._tenant)
            self._ensured = False

    async def run(self) -> dict[str, int]:
        """Walk the vault and incrementally update the Qdrant index.

        Returns::

            {"indexed": N, "deleted": M, "unchanged": K}

        where *N* notes were re-indexed, *M* were removed, and *K* were skipped
        because their content hash was unchanged.

        New/changed notes are embedded in batches across files (#230): their chunks
        accumulate into ``pending`` and flush once ``embed_batch_size`` chunks are
        queued, so the index completes in a handful of round-trips, not one per file.

        Serialised by ``self._run_lock`` so a watch-triggered pass (#232) and the startup
        index never walk the vault concurrently.
        """
        async with self._run_lock:
            return await self._run_walk()

    async def _run_walk(self) -> dict[str, int]:
        indexed = 0
        unchanged = 0

        seen_paths: set[str] = set()
        pending: list[_PendingNote] = []
        pending_chunks = 0

        # The read backend reports the vault root missing as "does not exist" — a
        # not-yet-provisioned ``knowledge/`` dir, not an error (a core outage *raises* from
        # ``md_entries`` below and the run retries, so an unreachable core never looks empty
        # and de-indexes everything).
        if not await self._reader.exists():
            log.warning("vault path does not exist")
            return {"indexed": 0, "deleted": 0, "unchanged": 0}

        model = await self._embedding_model()  # operator's choice, resolved once per run (#128)

        for entry in await self._reader.md_entries():
            rel = entry.path
            seen_paths.add(rel)

            mtime_ns = _mtime_ns(entry.mtime)
            existing = await self._notes.get(tenant=self._tenant, note_path=rel)

            if existing is not None and existing.mtime_ns == mtime_ns:
                # Fast path: mtime unchanged, skip reading the file.
                unchanged += 1
                continue

            content = await self._reader.read_text(rel)
            if content is None:
                # Vanished mid-walk, or unreadable (too large / binary via the file API).
                continue
            content_hash = _content_hash(content.encode("utf-8"))

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

            chunks = chunk_note(content, self._max_chars)
            indexed += 1
            if not chunks:
                # No embeddable content, but record the file so it isn't re-read
                # every run (mirrors _index_note returning 0).
                await self._notes.upsert(
                    tenant=self._tenant,
                    note_path=rel,
                    mtime_ns=mtime_ns,
                    content_hash=content_hash,
                    chunk_count=0,
                )
                continue

            pending.append(_PendingNote(rel, mtime_ns, content_hash, chunks))
            pending_chunks += len(chunks)
            log.debug("queued note", path=rel, chunks=len(chunks))
            if pending_chunks >= self._batch_size:
                await self._flush_batch(pending, model=model)
                pending.clear()
                pending_chunks = 0

        # Embed and persist any notes still queued below the batch threshold.
        await self._flush_batch(pending, model=model)
        pending.clear()

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
