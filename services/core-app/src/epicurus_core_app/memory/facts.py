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

# A new fact at least this cosine-similar to one already stored is treated as a duplicate
# and dropped. High enough that genuinely distinct facts are kept, low enough that the same
# fact phrased two ways from auto-extraction collapses to one.
_DEDUP_THRESHOLD = 0.92

# A rebuild (dimension-drift heal / re-embed) scans the collection in a single pass; a personal
# fact corpus is comfortably bounded, so this cap is generous. Hitting it is logged, never silent.
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

    async def _ensure(self, collection: str, dim: int) -> None:
        """Make sure *collection* exists and matches *dim*, reconciling a drifted one.

        A collection is created once per embedder dim; if the operator later switches to a
        model with a different output size, the *existing* collection would silently reject
        every query at the new dim (#436, ADR-0074). Detect that here — once per process
        lifetime per collection, via the ``_ensured`` cache — and reconcile by re-embedding
        stored facts rather than dropping them (facts are hand-distilled, not cheaply
        re-derived like a knowledge doc).
        """
        if collection in self._ensured:
            return
        if not await self._client.collection_exists(collection):
            await self._client.create_collection(
                collection, vectors_config=VectorParams(size=dim, distance=Distance.COSINE)
            )
            self._ensured.add(collection)
            return
        async with self._reconcile_lock:
            if collection in self._ensured:  # another task may have just reconciled it
                return
            info = await self._client.get_collection(collection)
            vectors_config = info.config.params.vectors
            current_dim = vectors_config.size if isinstance(vectors_config, VectorParams) else None
            if current_dim is not None and current_dim != dim:
                log.warning(
                    "facts collection dimension drift detected — embedder changed since this "
                    "collection was created; reconciling by re-embedding stored facts",
                    collection=collection,
                    old_dim=current_dim,
                    new_dim=dim,
                )
                migrated = await self._reembed_existing(collection, dim=dim)
                log.info("facts collection reconciled", collection=collection, migrated=migrated)
            self._ensured.add(collection)

    async def _reembed_existing(self, collection: str, *, dim: int | None = None) -> int:
        """Re-embed every fact in *collection* with the current embedder.

        Preserves each fact's id and metadata and replaces only the vector — contrast the
        knowledge module's drop-and-recrawl reconcile (ADR-0032/#332), which is safe there
        because a doc is cheaply re-read from its source file; a fact has no such source to
        recrawl. A single scroll pass up to ``_REBUILD_CAP``; hitting the cap is logged, never
        silent. When *dim* is given, the collection is always recreated at that size even with
        zero facts, so a caller with a known target dimension (:meth:`_ensure`'s drift-heal) is
        guaranteed a matching collection afterward; when it isn't (the manual :meth:`reembed_all`
        fan-out has no dim to hand until it sees a fact to embed), an empty collection is left
        untouched for :meth:`_ensure` to fix lazily on the next real save/search.
        """
        records: list[Record]
        records, _ = await self._client.scroll(
            collection_name=collection,
            with_payload=True,
            with_vectors=False,
            limit=self._rebuild_cap,
        )
        if len(records) >= self._rebuild_cap:
            log.warning(
                "fact re-embed hit the scan cap; some facts were not migrated",
                collection=collection,
                cap=self._rebuild_cap,
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
        """Return up to ``limit`` facts most similar to ``query``, with match scores."""
        collection = scope_collection(self._base, tenant)
        if not await self._client.collection_exists(collection):
            return []
        vector = (await self._embed([query]))[0]
        # Recall queries the collection directly (unlike save, it has no reason to otherwise
        # touch _ensure) — without this, a drifted collection 400s here instead of healing.
        await self._ensure(collection, len(vector))
        result = await self._client.query_points(
            collection_name=collection, query=vector, limit=limit, with_payload=True
        )
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
