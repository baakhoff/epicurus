"""Durable facts about the user — the semantic memory the assistant carries across chats.

Unlike a raw conversation snippet, a *fact* is a short standalone statement the assistant
keeps about the operator: an identity detail, a stable preference, an ongoing project
("Works on a local-first assistant called epicurus", "Prefers metric units"). Facts are
written two ways (ADR-0045), mirroring the industry pattern — ChatGPT's *saved memories*,
Mem0's extract-then-consolidate, LangMem's hot-path-plus-background:

* the agent's ``remember`` tool — explicit, when the user says "remember…" or the model
  decides a durable detail is worth keeping (the *hot path*);
* a background extraction pass after each turn — automatic, distilling new facts from the
  exchange without adding latency to the reply (the *background path*).

Both land here: embedded and stored tenant-scoped in Qdrant. A write that closely matches an
existing fact is dropped (a cheap single-vector dedup, so auto-extraction does not re-save
the same fact every turn). Recall searches these facts and injects the closest into the next
turn, and the Settings → Memory view lists them for inspection and forgetting.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    PointIdsList,
    PointStruct,
    Record,
    VectorParams,
)

from epicurus_core import get_logger
from epicurus_core.tenancy import scope_collection

log = get_logger("epicurus_core_app.memory.facts")

Embedder = Callable[[list[str]], Awaitable[list[list[float]]]]

#: How a fact was written — the agent's ``remember`` tool, or background extraction.
SOURCE_TOOL = "tool"
SOURCE_AUTO = "auto"

#: The recovery the operator can run by hand for *recall* memory. Deliberately not the
#: Models page's "Re-embed everything": that fans out to the modules' ``/reindex`` only
#: (``ModuleRegistry.reembed``) and never touches the fact collection, which is rebuilt by the
#: ``facts-reembed`` maintenance job, which the Maintenance card's "Run maintenance now"
#: includes. Naming the wrong button would be exactly the failure this issue is about.
#: Closing the gap — one action covering modules *and* facts — is a follow-up (#944).
REBUILD_CURE = "run “Run maintenance now” under Settings → Maintenance"


class RecallDimensionError(RuntimeError):
    """Recall/save could not be served because the facts collection's width is wrong.

    Carries a message written for the operator — never Qdrant's raw rejection text, and
    never the tenant-scoped collection name (that goes in the log's structured fields).
    """


class EmbeddingDimensionChanged(RecallDimensionError):
    """The stored vectors were built by a differently-sized embedder and no heal ran."""


class EmbeddingDimensionUnreadable(RecallDimensionError):
    """The collection's vector configuration could not be read as a single width.

    Raised instead of the silent no-op that #944 traced: a shape the check does not
    recognise used to leave ``current_dim = None``, skip the reconcile, and still cache the
    collection as reconciled — so every later query repeated the same doomed request for the
    process's life. Nothing is cached on this path; the next call retries.
    """


def _changed_message(stored_dim: int, expected_dim: int) -> str:
    """The operator-facing sentence for a width mismatch (no ids, no provider payload)."""
    return (
        f"embedding dimension changed {stored_dim}→{expected_dim}; "
        f"recall memory needs a rebuild — {REBUILD_CURE}"
    )


def vector_size(vectors_config: object) -> int | None:
    """The single vector width a collection is configured for, or ``None``.

    ``CollectionParams.vectors`` is ``VectorParams | dict[str, VectorParams] | None``: an
    unnamed collection reports the first, a *named*-vector one the second. We only ever
    create unnamed collections, but a server (or client version) that reports the single
    unnamed vector as a one-entry mapping must not read as "unknown" — that was the shape
    the reconcile in #944 failed to recognise. A genuinely multi-named configuration stays
    ``None``: we cannot pick a width for it, and guessing would drop someone's data.
    """
    if isinstance(vectors_config, VectorParams):
        return vectors_config.size
    if isinstance(vectors_config, dict) and len(vectors_config) == 1:
        (only,) = vectors_config.values()
        if isinstance(only, VectorParams):
            return only.size
    return None


class RecallDimensionState(BaseModel):
    """What the fact store last observed about its collection's vector width (#944).

    Process-local and observation-based — it reports what a save/recall actually saw, not a
    probe run on demand, so reading it costs nothing and never embeds. ``status``:

    * ``ok`` — nothing has gone wrong since this process started.
    * ``healed`` — a change was found and the collection was rebuilt at the new width.
    * ``changed`` — a change was found and the rebuild has not (yet) succeeded.
    * ``unreadable`` — the collection's vector configuration is not a single width.
    """

    status: Literal["ok", "healed", "changed", "unreadable"] = "ok"
    stored_dim: int | None = None
    expected_dim: int | None = None
    detail: str = ""


# A new fact at least this cosine-similar to one already stored is treated as a duplicate
# and dropped. High enough that genuinely distinct facts are kept, low enough that the same
# fact phrased two ways from auto-extraction collapses to one.
_DEDUP_THRESHOLD = 0.92

# A rebuild (dimension-drift heal / re-embed) pages through the collection this many points at
# a time. This bounds memory per page, not the total corpus — the scroll loops on Qdrant's
# offset until every point has been visited, however many pages that takes (#450).
_REBUILD_CAP = 10_000


class UserFact(BaseModel):
    """One durable fact about the user. ``id`` is an opaque UUID (the Qdrant point id)."""

    id: str
    text: str
    source: str = SOURCE_AUTO
    created_at: datetime | None = None


class UserFactHit(UserFact):
    """A fact returned by a similarity search, carrying its match ``score``."""

    score: float


class UserFactStore:
    """Stores and retrieves durable user facts by embedding similarity (tenant-scoped)."""

    def __init__(
        self,
        client: AsyncQdrantClient,
        embed: Embedder,
        *,
        base_collection: str = "facts",
        rebuild_cap: int = _REBUILD_CAP,
    ) -> None:
        self._client = client
        self._embed = embed
        self._base = base_collection
        self._rebuild_cap = rebuild_cap
        self._ensured: set[str] = set()
        # Guards the reconcile path only (collection exists + may need a dim check/rebuild) —
        # the far hotter "already ensured" and "brand new" paths below never touch it.
        self._reconcile_lock = asyncio.Lock()
        # Strong references to in-flight reconciles. The heal is shielded from the caller's
        # cancellation (see _ensure), and a shielded task with no strong reference anywhere can
        # be garbage-collected mid-flight — which is precisely the window in which the
        # collection has been dropped and not yet refilled.
        self._reconciling: set[asyncio.Task[None]] = set()
        # Per *collection*, therefore per tenant (constraint #1): a width observed while
        # serving one tenant is not a fact about another's collection, and must never be
        # reported to — or cleared by — anyone else.
        self._dimension_state: dict[str, RecallDimensionState] = {}

    def recall_dimension(self, *, tenant: str) -> RecallDimensionState:
        """What this process last observed about *tenant*'s facts collection width (#944).

        Free to call: it reports an observation already made by a save or a recall, and never
        embeds or queries. Surfaced on the Models page next to the maintenance job that clears
        a ``changed`` state the lazy heal could not.
        """
        return self._dimension_state.get(
            scope_collection(self._base, tenant), RecallDimensionState()
        ).model_copy()

    async def _ensure(self, collection: str, dim: int) -> None:
        """Make sure *collection* exists and matches *dim*, reconciling a drifted one.

        A collection is created once per embedder dim; if the operator later switches to a
        model with a different output size, the *existing* collection would silently reject
        every query at the new dim (#436, ADR-0074). Detect that here — once per process
        lifetime per collection, via the ``_ensured`` cache — and reconcile by re-embedding
        stored facts rather than dropping them (facts are hand-distilled, not cheaply
        re-derived like a knowledge doc).

        Two invariants the original lacked, both from #944 (ADR-0141):

        * ``_ensured`` records a width that was **confirmed**, never merely looked at. A
          reconcile that was skipped or that failed leaves the collection uncached, so the
          next save/recall retries the fix instead of repeating a doomed query forever.
        * the reconcile is **shielded** from the caller's cancellation. Recall is time-boxed
          (``MEMORY_RECALL_TIMEOUT_S``, ADR-0051) and the heal drops the collection before
          refilling it — a cancellation landing in that window would take the operator's whole
          fact corpus with it. The caller still gives up on its budget; the heal finishes, and
          the next turn's recall finds a healthy collection.
        """
        if collection in self._ensured:
            return
        if not await self._client.collection_exists(collection):
            await self._client.create_collection(
                collection, vectors_config=VectorParams(size=dim, distance=Distance.COSINE)
            )
            self._ensured.add(collection)
            return
        task = asyncio.ensure_future(self._reconcile(collection, dim))
        self._reconciling.add(task)
        task.add_done_callback(self._forget_reconcile)
        await asyncio.shield(task)

    def _forget_reconcile(self, task: asyncio.Task[None]) -> None:
        """Drop a finished reconcile, retrieving any error it raised after its caller left.

        A caller that gave up on its recall budget is no longer awaiting the shielded task, so
        without this the failure would surface as asyncio's "Task exception was never retrieved"
        noise at garbage-collection time. It is already logged where it happened.
        """
        self._reconciling.discard(task)
        if not task.cancelled():
            task.exception()

    async def _reconcile(self, collection: str, dim: int) -> None:
        """Confirm an existing collection's width against *dim*, healing a drifted one.

        Runs as its own task so :meth:`_ensure` can shield it; holds the reconcile lock for the
        whole check-and-heal so two concurrent turns cannot rebuild the same collection twice.
        """
        async with self._reconcile_lock:
            if collection in self._ensured:  # another task may have just reconciled it
                return
            info = await self._client.get_collection(collection)
            current_dim = vector_size(info.config.params.vectors)
            if current_dim is None:
                detail = (
                    "the recall collection's vector configuration could not be read as a "
                    f"single width, so it cannot be reconciled with the current {dim}-d "
                    f"embedding model — {REBUILD_CURE}"
                )
                self._dimension_state[collection] = RecallDimensionState(
                    status="unreadable", expected_dim=dim, detail=detail
                )
                log.error(
                    "facts collection vector configuration is not a single width; "
                    "cannot confirm it against the current embedder — not caching it",
                    collection=collection,
                    new_dim=dim,
                )
                raise EmbeddingDimensionUnreadable(detail)
            if current_dim != dim:
                log.warning(
                    "facts collection dimension drift detected — embedder changed since this "
                    "collection was created; reconciling by re-embedding stored facts",
                    collection=collection,
                    old_dim=current_dim,
                    new_dim=dim,
                )
                self._dimension_state[collection] = RecallDimensionState(
                    status="changed",
                    stored_dim=current_dim,
                    expected_dim=dim,
                    detail=_changed_message(current_dim, dim),
                )
                migrated = await self._reembed_existing(collection, dim=dim)
                log.info("facts collection reconciled", collection=collection, migrated=migrated)
                self._dimension_state[collection] = RecallDimensionState(
                    status="healed",
                    stored_dim=current_dim,
                    expected_dim=dim,
                    detail=(
                        f"recall memory was rebuilt from {current_dim}-d to {dim}-d vectors "
                        f"({migrated} fact(s) re-embedded) after the embedding model changed"
                    ),
                )
            self._ensured.add(collection)

    async def _named_dimension_error(
        self, collection: str, dim: int
    ) -> RecallDimensionError | None:
        """A named width-mismatch error for a rejected query, or ``None`` if that wasn't it.

        Qdrant answers a width mismatch with an opaque ``Vector dimension error`` that no retry
        fixes, and #879 asks that the operator see the cause and the cure instead of that raw
        text. Rather than pattern-matching a provider's wording, this re-reads the collection's
        configured width on the *error* path only and compares: if it really no longer matches,
        the mismatch is named and the stale ``_ensured`` entry is dropped so the next call
        re-runs the heal.
        """
        try:
            info = await self._client.get_collection(collection)
        except Exception:  # the collection is gone or Qdrant is down — not our diagnosis
            return None
        stored = vector_size(info.config.params.vectors)
        if stored is None or stored == dim:
            return None
        self._ensured.discard(collection)
        self._dimension_state[collection] = RecallDimensionState(
            status="changed",
            stored_dim=stored,
            expected_dim=dim,
            detail=_changed_message(stored, dim),
        )
        log.warning(
            "recall rejected: the collection's vectors were built by a different embedder",
            collection=collection,
            old_dim=stored,
            new_dim=dim,
        )
        return EmbeddingDimensionChanged(_changed_message(stored, dim))

    async def _reembed_existing(self, collection: str, *, dim: int | None = None) -> int:
        """Re-embed every fact in *collection* with the current embedder.

        Preserves each fact's id and metadata and replaces only the vector — contrast the
        knowledge module's drop-and-recrawl reconcile (ADR-0032/#332), which is safe there
        because a doc is cheaply re-read from its source file; a fact has no such source to
        recrawl. Pages through the *entire* collection ``_rebuild_cap`` points at a time,
        following Qdrant's returned offset until exhausted — however many facts are stored,
        every one is preserved; ``_rebuild_cap`` bounds only how many points sit in memory per
        page (#450; a single-pass scroll used to silently drop anything past the cap). When
        *dim* is given, the collection is always recreated at that size even with zero facts,
        so a caller with a known target dimension (:meth:`_ensure`'s drift-heal) is guaranteed a
        matching collection afterward; when it isn't (the manual :meth:`reembed_all` fan-out has
        no dim to hand until it sees a fact to embed), an empty collection is left untouched for
        :meth:`_ensure` to fix lazily on the next real save/search.
        """
        records: list[Record] = []
        pages = 0
        page, offset = await self._client.scroll(
            collection_name=collection,
            with_payload=True,
            with_vectors=False,
            limit=self._rebuild_cap,
        )
        records.extend(page)
        pages += 1
        while offset is not None:
            page, offset = await self._client.scroll(
                collection_name=collection,
                with_payload=True,
                with_vectors=False,
                limit=self._rebuild_cap,
                offset=offset,
            )
            records.extend(page)
            pages += 1
        if pages > 1:
            log.info(
                "facts re-embed paginated the full collection",
                collection=collection,
                pages=pages,
                facts=len(records),
            )

        vectors: list[list[float]] = []
        if records:
            texts = [str((record.payload or {}).get("text", "")) for record in records]
            vectors = await self._embed(texts)
            dim = len(vectors[0])
        if dim is None:
            return 0

        await self._client.delete_collection(collection)
        await self._client.create_collection(
            collection, vectors_config=VectorParams(size=dim, distance=Distance.COSINE)
        )
        if records:
            await self._client.upsert(
                collection_name=collection,
                points=[
                    PointStruct(id=record.id, vector=vector, payload=record.payload or {})
                    for record, vector in zip(records, vectors, strict=True)
                ],
            )
        return len(records)

    async def reembed_all(self, *, tenant: str) -> int:
        """Force a re-embed of the tenant's stored facts with the current embedder.

        The manual "Re-embed everything" fan-out (ADR-0054) calls this so a model swap
        refreshes memory the same way it refreshes knowledge/notes, rather than leaving facts
        to heal lazily the next time a save or recall happens to touch this collection.
        Returns the number of facts re-embedded (0 if the tenant has none yet).
        """
        collection = scope_collection(self._base, tenant)
        if not await self._client.collection_exists(collection):
            return 0
        async with self._reconcile_lock:
            migrated = await self._reembed_existing(collection)
            if migrated:
                self._ensured.add(collection)
            # The operator ran the documented cure; whatever this process had observed about a
            # stale width no longer holds, so the Models page stops reporting it (#944).
            self._dimension_state.pop(collection, None)
        return migrated

    async def save(self, *, tenant: str, text: str, source: str = SOURCE_AUTO) -> UserFact | None:
        """Save a fact, returning it — or ``None`` when it duplicates an existing one.

        Embeds ``text`` once and reuses that vector both to dedup (a near-identical fact is
        a no-op) and to store, so a save costs a single embedding call.
        """
        text = text.strip()
        if not text:
            return None
        vector = (await self._embed([text]))[0]
        collection = scope_collection(self._base, tenant)
        await self._ensure(collection, len(vector))

        existing = await self._client.query_points(
            collection_name=collection, query=vector, limit=1, with_payload=False
        )
        if existing.points and existing.points[0].score >= _DEDUP_THRESHOLD:
            return None

        fact_id = str(uuid.uuid4())
        created = datetime.now(UTC)
        await self._client.upsert(
            collection_name=collection,
            points=[
                PointStruct(
                    id=fact_id,
                    vector=vector,
                    payload={
                        "text": text,
                        "source": source,
                        "created_at": created.isoformat(),
                    },
                )
            ],
        )
        return UserFact(id=fact_id, text=text, source=source, created_at=created)

    async def count(self, *, tenant: str) -> int:
        """How many facts the tenant's collection holds (0 if it doesn't exist)."""
        collection = scope_collection(self._base, tenant)
        if not await self._client.collection_exists(collection):
            return 0
        return (await self._client.count(collection_name=collection)).count

    async def list_facts(self, *, tenant: str, limit: int = 200, cap: int = 2000) -> list[UserFact]:
        """The tenant's facts, newest first, capped at ``limit``.

        Qdrant scroll has no global ordering, so up to ``cap`` points are scrolled and sorted
        by their stored ``created_at`` (a personal assistant's fact corpus is bounded). The
        route pairs this with :meth:`count` so the UI can say how much isn't shown.
        """
        collection = scope_collection(self._base, tenant)
        if not await self._client.collection_exists(collection):
            return []
        records, _ = await self._client.scroll(
            collection_name=collection, with_payload=True, with_vectors=False, limit=cap
        )
        facts = [self._to_fact(str(record.id), record.payload or {}) for record in records]
        facts.sort(key=lambda f: f.created_at or datetime.min.replace(tzinfo=UTC), reverse=True)
        return facts[:limit]

    async def search(self, *, tenant: str, query: str, limit: int = 8) -> list[UserFactHit]:
        """Return up to ``limit`` facts most similar to ``query``, with match scores.

        Raises :class:`RecallDimensionError` when the collection's vectors were built by a
        differently-sized embedder and could not be healed — a named cause and cure rather
        than Qdrant's raw rejection text (#879).
        """
        collection = scope_collection(self._base, tenant)
        if not await self._client.collection_exists(collection):
            return []
        vector = (await self._embed([query]))[0]
        # Recall queries the collection directly (unlike save, it has no reason to otherwise
        # touch _ensure) — without this, a drifted collection 400s here instead of healing.
        await self._ensure(collection, len(vector))
        try:
            result = await self._client.query_points(
                collection_name=collection, query=vector, limit=limit, with_payload=True
            )
        except Exception as exc:
            named = await self._named_dimension_error(collection, len(vector))
            if named is not None:
                raise named from exc
            raise
        return [
            UserFactHit(
                **self._to_fact(str(point.id), point.payload or {}).model_dump(),
                score=float(point.score),
            )
            for point in result.points
            if point.payload
        ]

    async def recall(self, *, tenant: str, query: str, limit: int = 8) -> list[str]:
        """The agent's recall path: just the text of the most-relevant facts."""
        hits = await self.search(tenant=tenant, query=query, limit=limit)
        return [hit.text for hit in hits]

    async def forget(self, *, tenant: str, fact_id: str) -> int:
        """Forget one fact so it stops being recalled. Returns 1 if the collection exists."""
        collection = scope_collection(self._base, tenant)
        if not await self._client.collection_exists(collection):
            return 0
        await self._client.delete(
            collection_name=collection, points_selector=PointIdsList(points=[fact_id])
        )
        return 1

    @staticmethod
    def _to_fact(fact_id: str, payload: dict[str, object]) -> UserFact:
        raw_created = payload.get("created_at")
        created: datetime | None = None
        if isinstance(raw_created, str):
            try:
                created = datetime.fromisoformat(raw_created)
            except ValueError:
                created = None
        return UserFact(
            id=fact_id,
            text=str(payload.get("text", "")),
            source=str(payload.get("source", SOURCE_AUTO)),
            created_at=created,
        )
