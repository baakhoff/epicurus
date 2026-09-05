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
    FieldCondition,
    Filter,
    FilterSelector,
    MatchValue,
    PointStruct,
)

from epicurus_core import PlatformClient, get_logger, scope_collection
from epicurus_knowledge.chunker import Chunk, chunk_note
from epicurus_knowledge.db import DocIndex, NoteIndex
from epicurus_knowledge.dimensions import CollectionDimensionGuard, EmbeddingDimensionChanged
from epicurus_knowledge.fuse import FusePolicy, FuseTrip, IndexFuse
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
        reader: The vault read backend (#346, ADR-0070). The default (normal mode) is an
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
        fuse_policy: Thresholds for the mass de-index fuse (#848) — a pass that would
            delete an anomalous share of the ledger, or any pass over a source that reads
            empty while the ledger is not, is refused instead of reconciling a stale mount
            into a wipe. Defaults to :class:`~epicurus_knowledge.fuse.FusePolicy`'s own
            defaults; ``force=True`` on a call bypasses it.
        dimensions: The guard that owns this Qdrant collection's vector size (#865). Pass the
            **same** guard to every source writing the same collection — the bundled platform
            docs and the per-module docs share ``<tenant>__docs`` — so recreating it at a new
            embedding dimension clears all of their ledgers. Defaults to a private guard,
            which is right for a source that owns its collection alone (the vault).
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
        fuse_policy: FusePolicy | None = None,
        dimensions: CollectionDimensionGuard | None = None,
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
        # Owns this collection's vector size (#865). Shared when another source writes the same
        # collection (the bundled docs and the module docs both use ``<tenant>__docs``), so a
        # recreate clears every claiming ledger; private when this source owns it alone.
        self._dimensions = dimensions or CollectionDimensionGuard(qdrant, self._collection)
        self._dimensions.register_reset(self._clear_ledger)
        # The mass de-index fuse for this source (#848). Public: the app reads its state for
        # GET /status, and POST /reindex asks it for a verdict before resetting anything.
        self.fuse = IndexFuse(tenant=tenant, source=collection_base, policy=fuse_policy)
        # Serialises full re-index passes on this indexer instance. The vault indexer is
        # shared between the startup runner (#230) and the live watcher (#232), and the
        # Re-index action can fire mid-startup; without this two concurrent walks could
        # double-embed or race the ledger. Held only by run(); single-file index_path /
        # remove_path stay lock-free (in watch mode the vault is read-only, so they and a
        # watch pass never overlap).
        self._run_lock = asyncio.Lock()

    async def _clear_ledger(self) -> None:
        """Drop every ledger row for this source — the guard's recreate hook (#865)."""
        await self._notes.clear(tenant=self._tenant)

    async def _verify_dimension(self, model: str | None) -> None:
        """Confirm the existing collection's width against the current embedder (#865).

        One short embed per pass, deliberately **not** cached across passes: a pass in which
        every file reads unchanged writes nothing, so it would otherwise sail straight past a
        model switch that has already broken search — and the switch this exists to catch
        happens *while the process is running*, so an answer remembered from an earlier pass
        would be blind to exactly the case it is for. Skipped entirely on a fresh install (no
        collection yet — the first upsert creates it at the right size), which is the only
        state in which it costs nothing. Raises :class:`EmbeddingDimensionChanged` on a
        mismatch, which the caller turns into a full rebuild.
        """
        if not await self._qdrant.collection_exists(self._collection):
            return
        probe = await self._platform.embed(["dimension probe"], model=model)
        await self._dimensions.ensure(len(probe[0]))

    async def _ensure_collection(self, dim: int) -> None:
        """Create the collection on first use, or heal it when the embedder's size changed.

        Delegated to the shared :class:`CollectionDimensionGuard` so a source that shares a
        collection with another (``<tenant>__docs``) heals both ledgers at once. Raises
        :class:`EmbeddingDimensionChanged` after a heal; ``run`` / ``index_path`` catch it and
        retry, which is what turns the heal into a full rebuild rather than a partial pass.
        """
        await self._dimensions.ensure(dim)

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

        If this is the first write since the embedding model's vector size changed, the guard
        recreates the collection and clears the ledger (#865); the single retry below then
        lands this file in the fresh collection. Every *other* file is re-embedded by the next
        full ``run``, which the empty ledger now forces.
        """
        content = await self._reader.read_text(rel)
        if content is None:
            raise FileNotFoundError(rel)
        content_hash = _content_hash(content.encode("utf-8"))
        if await self._notes.get(tenant=self._tenant, note_path=rel) is not None:
            await self._delete_note_vectors(rel)
        model = await self._embedding_model()
        try:
            chunk_count = await self._index_note(rel, content, model=model)
        except EmbeddingDimensionChanged:
            chunk_count = await self._index_note(rel, content, model=model)
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

    async def move_path(self, from_rel: str, to_rel: str) -> bool:
        """Re-key the index after *from_rel* was already renamed/moved to *to_rel* on disk.

        The move itself (the file write) is the caller's job — the editor's move endpoint
        and an approved move suggestion (ADR-0033) both rename via the platform file API
        first, then call this to keep the ledger + Qdrant in step. Without it the old
        identity lingers as a phantom search hit indefinitely: nothing else notices a moved
        path until the next full ``run`` (#470).

        A single ``.md`` file swaps its vectors directly: drop the old path, then re-embed
        under the new one (point ids are derived from ``note_path``, so they must be
        recomputed even though the content is unchanged). A folder move renames many notes
        at once, so the incremental walk in :meth:`run` reconciles the whole subtree in one
        pass instead. Best-effort like :meth:`index_path`: a failure here must never undo an
        already-successful move, so it is logged rather than raised. Returns whether the
        re-index succeeded.
        """
        if from_rel.endswith(".md"):
            await self.remove_path(from_rel)
            try:
                await self.index_path(to_rel)
                return True
            except Exception as exc:
                log.warning("move applied but re-index failed", path=to_rel, error=str(exc))
                return False
        try:
            await self.run()
            return True
        except Exception as exc:
            log.warning("folder move applied but re-index failed", path=to_rel, error=str(exc))
            return False

    async def reconcile(self, *, force: bool = False) -> bool:
        """Self-heal after a Qdrant reset (#229), or GC ledger rows the vault no longer has.

        qdrant vectors are derived data and may be wiped on a server upgrade (see the
        ``qdrant-init`` guard). If our collection is gone but the Postgres ledger still
        lists files as indexed, the incremental walk would skip every file and leave the
        collection empty. Detect that drift and clear the ledger so the next ``run``
        re-embeds from scratch.

        Must run for *all* sources before any ``run`` recreates a collection — the vault
        and module-docs share ``<tenant>__docs`` with the platform docs, so the runner
        reconciles every source up front (see :class:`runner.IndexRunner`).

        When the collection is intact, GC instead (#470): drop any ledger row whose path the
        live vault no longer has. A cheap, no-re-embed safety net for anything that orphans a
        path without going through :meth:`move_path` / :meth:`remove_path`, so a stale entry
        is never more than one reconcile pass (every startup and retry) from clearing itself.

        The GC half is a de-index, so it is fused (#848): it drops rows for paths the source
        no longer reports, which is exactly what a stale mount fakes. ``force`` bypasses the
        fuse for this pass (the ledger-clearing half is not fused — it only discards rows
        whose vectors are already gone).

        Returns ``True`` when it changed anything (cleared the ledger, or GC'd stale rows).
        """
        if not await self._qdrant.collection_exists(self._collection):
            known = await self._notes.count(tenant=self._tenant)
            if known == 0:
                return False
            log.warning(
                "qdrant collection missing but ledger non-empty; clearing ledger to re-index",
                collection=self._collection,
                ledger_rows=known,
            )
            await self._notes.clear(tenant=self._tenant)
            self._dimensions.forget()
            return True
        return await self._gc_stale(force=force)

    async def _gc_stale(self, *, force: bool = False) -> bool:
        """Drop ledger rows (+ their vectors) whose path no longer exists in the live vault.

        One ``stat`` per ledger row and no content reads or re-embeds, so this is cheap
        relative to a full :meth:`run` — a reconcile-time complement to it rather than a
        replacement. Returns ``True`` if anything was removed.

        Fused (#848): the whole set of missing paths is collected first and weighed against
        the ledger, so a source that has gone wholesale missing refuses the GC rather than
        performing it one cheap ``stat`` at a time. How full the vault *itself* is takes one
        extra walk, paid only when there is something to GC — the fuse must not read "the
        ledger's live rows" as "the source", or a lone orphan in a healthy vault would look
        like a vanished source.
        """
        known = await self._notes.list_paths(tenant=self._tenant)
        missing = [rel for rel in known if await self._reader.stat(rel) is None]
        if not missing:
            return False
        entries = await self._reader.md_entries() if await self._reader.exists() else []
        trip = self.fuse.evaluate(
            ledger_rows=len(known),
            would_delete=len(missing),
            source_entries=len(entries),
            force=force,
        )
        if trip is not None:
            self.fuse.trip(trip)
            return False
        removed = 0
        for rel in missing:
            await self._delete_note_vectors(rel)
            await self._notes.delete(tenant=self._tenant, note_path=rel)
            removed += 1
        if removed:
            log.warning(
                "gc: removed ledger rows for paths no longer in the vault",
                collection=self._collection,
                count=removed,
            )
        return removed > 0

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
            self._dimensions.forget()

    async def check_source_fuse(self, *, force: bool = False) -> FuseTrip | None:
        """Would rebuilding from the source right now be a mass de-index? (#848)

        The read-only question behind ``POST /reindex``'s pre-check: :meth:`reset` empties
        the ledger by design, so by the time the rebuild walks the source there is nothing
        left for the fuse to protect. Asking first — *before* anything is dropped — is what
        keeps a re-index against a stale mount from completing the wipe the incident
        started. Returns the verdict without recording it; the caller decides.
        """
        return await self._walk_verdict(force=force)

    async def _walk_verdict(self, *, force: bool = False) -> FuseTrip | None:
        """The fuse verdict for a full walk: ledger rows the source no longer reports."""
        if not await self._reader.exists():
            entries: set[str] = set()
        else:
            entries = {entry.path for entry in await self._reader.md_entries()}
        known = set(await self._notes.list_paths(tenant=self._tenant))
        return self.fuse.evaluate(
            ledger_rows=len(known),
            would_delete=len(known - entries),
            source_entries=len(entries),
            force=force,
        )

    async def run(self, *, force: bool = False) -> dict[str, int]:
        """Walk the vault and incrementally update the Qdrant index.

        Returns::

            {"indexed": N, "deleted": M, "unchanged": K, "fuse_tripped": 0}

        where *N* notes were re-indexed, *M* were removed, and *K* were skipped
        because their content hash was unchanged.

        New/changed notes are embedded in batches across files (#230): their chunks
        accumulate into ``pending`` and flush once ``embed_batch_size`` chunks are
        queued, so the index completes in a handful of round-trips, not one per file.

        A pass whose deletions trip the mass de-index fuse (#848) is abandoned whole —
        ``fuse_tripped`` comes back ``1``, every other count is ``0``, and neither the
        ledger nor the collection is touched. The adds are dropped along with the deletes
        deliberately: a source that looks like a stale mount is not a source to index
        *from* either. ``force=True`` skips the fuse for this pass.

        A pass that discovers the embedding model's vector size has changed (#865) heals the
        collection — recreated at the new size, every ledger claiming it cleared — and then
        **walks again from the top**. The second walk sees an empty ledger and a matching
        collection, so it re-embeds the whole source in this one call; its counts are what is
        returned, the abandoned first walk's being meaningless (its vectors and rows are gone).
        The heal can happen at most once per pass: afterwards the guard's cached size matches.

        Serialised by ``self._run_lock`` so a watch-triggered pass (#232) and the startup
        index never walk the vault concurrently.
        """
        async with self._run_lock:
            try:
                return await self._run_walk(force=force)
            except EmbeddingDimensionChanged as exc:
                log.warning(
                    "embedding dimension changed mid-pass; re-walking the source from scratch",
                    collection=self._collection,
                    detail=str(exc),
                )
                return await self._run_walk(force=force)

    async def _run_walk(self, *, force: bool = False) -> dict[str, int]:
        indexed = 0
        unchanged = 0

        seen_paths: set[str] = set()
        pending: list[_PendingNote] = []
        pending_chunks = 0

        # The read backend reports the vault root missing as "does not exist" — a
        # not-yet-provisioned ``knowledge/`` dir, not an error (a core outage *raises* from
        # ``md_entries`` below and the run retries, so an unreachable core never looks empty
        # and de-indexes everything).
        exists = await self._reader.exists()
        entries = await self._reader.md_entries() if exists else []

        # The mass de-index fuse (#848), weighed before a single row is touched: an absent or
        # empty-reading source with a populated ledger is a stale mount until proven otherwise,
        # and a pass that would delete most of the ledger is one too. Refusing here — rather
        # than at the delete loop below — is what makes the refusal atomic: nothing has been
        # embedded, upserted, or dropped yet, so the ledger and collection stay exactly as
        # they were.
        known_before = set(await self._notes.list_paths(tenant=self._tenant))
        seen_now = {entry.path for entry in entries}
        trip = self.fuse.evaluate(
            ledger_rows=len(known_before),
            would_delete=len(known_before - seen_now),
            source_entries=len(entries),
            force=force,
        )
        if trip is not None:
            self.fuse.trip(trip)
            return {"indexed": 0, "deleted": 0, "unchanged": 0, "fuse_tripped": 1}

        if not exists:
            log.warning("vault path does not exist")
            self.fuse.clear()
            return {"indexed": 0, "deleted": 0, "unchanged": 0, "fuse_tripped": 0}

        model = await self._embedding_model()  # operator's choice, resolved once per run (#128)
        # Settle the collection's vector width before the walk, so a pass in which everything
        # reads unchanged still notices a switched embedding model (#865). Weighed *after* the
        # fuse, so a stale mount is still refused before anything is touched.
        await self._verify_dimension(model)

        for entry in entries:
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
        # The pass reconciled without tripping — re-arm so a past trip stops showing on
        # GET /status and the metric returns to 0.
        self.fuse.clear()
        return {
            "indexed": indexed,
            "deleted": deleted,
            "unchanged": unchanged,
            "fuse_tripped": 0,
        }
