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

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from pydantic import BaseModel
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    PointIdsList,
    PointStruct,
    VectorParams,
)

from epicurus_core.tenancy import scope_collection

Embedder = Callable[[list[str]], Awaitable[list[list[float]]]]

#: How a fact was written — the agent's ``remember`` tool, or background extraction.
SOURCE_TOOL = "tool"
SOURCE_AUTO = "auto"

# A new fact at least this cosine-similar to one already stored is treated as a duplicate
# and dropped. High enough that genuinely distinct facts are kept, low enough that the same
# fact phrased two ways from auto-extraction collapses to one.
_DEDUP_THRESHOLD = 0.92


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
        self, client: AsyncQdrantClient, embed: Embedder, *, base_collection: str = "facts"
    ) -> None:
        self._client = client
        self._embed = embed
        self._base = base_collection
        self._ensured: set[str] = set()

    async def _ensure(self, collection: str, dim: int) -> None:
        if collection in self._ensured:
            return
        if not await self._client.collection_exists(collection):
            await self._client.create_collection(
                collection, vectors_config=VectorParams(size=dim, distance=Distance.COSINE)
            )
        self._ensured.add(collection)

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
