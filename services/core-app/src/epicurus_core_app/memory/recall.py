"""Semantic recall — embed conversation text and retrieve it from Qdrant (tenant-scoped)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from pydantic import BaseModel
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchValue,
    PointIdsList,
    PointStruct,
    VectorParams,
)

from epicurus_core.tenancy import scope_collection

Embedder = Callable[[list[str]], Awaitable[list[list[float]]]]


class RecallPoint(BaseModel):
    """One stored memory snippet. ``id`` is the source ``agent_messages.id`` (the point id)."""

    id: int
    session_id: str
    text: str


class RecallHit(RecallPoint):
    """A snippet returned by a similarity search, carrying its match ``score``."""

    score: float


class SemanticRecall:
    """Stores and retrieves conversation snippets by embedding similarity."""

    def __init__(
        self, client: AsyncQdrantClient, embed: Embedder, *, base_collection: str = "memory"
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

    async def index(self, *, tenant: str, session_id: str, text: str, point_id: int) -> None:
        """Embed ``text`` and upsert it into the tenant's collection."""
        vector = (await self._embed([text]))[0]
        collection = scope_collection(self._base, tenant)
        await self._ensure(collection, len(vector))
        await self._client.upsert(
            collection_name=collection,
            points=[
                PointStruct(
                    id=point_id, vector=vector, payload={"session_id": session_id, "text": text}
                )
            ],
        )

    async def count(self, *, tenant: str) -> int:
        """How many snippets the tenant's collection holds (0 if it doesn't exist)."""
        collection = scope_collection(self._base, tenant)
        if not await self._client.collection_exists(collection):
            return 0
        return (await self._client.count(collection_name=collection)).count

    async def list_points(
        self, *, tenant: str, limit: int = 100, cap: int = 1000
    ) -> list[RecallPoint]:
        """The tenant's snippets, newest first (point id ≈ chronological), capped at ``limit``.

        Scrolls up to ``cap`` points (Qdrant scroll has no global ordering) and returns the
        ``limit`` highest ids. The corpus of a personal assistant is bounded; the route pairs
        this with :meth:`count` so the UI can show how much is not shown rather than truncate
        silently.
        """
        collection = scope_collection(self._base, tenant)
        if not await self._client.collection_exists(collection):
            return []
        records, _ = await self._client.scroll(
            collection_name=collection, with_payload=True, with_vectors=False, limit=cap
        )
        points = [
            RecallPoint(
                id=int(record.id),
                session_id=str((record.payload or {}).get("session_id", "")),
                text=str((record.payload or {}).get("text", "")),
            )
            for record in records
        ]
        points.sort(key=lambda point: point.id, reverse=True)
        return points[:limit]

    async def search(self, *, tenant: str, query: str, limit: int = 20) -> list[RecallHit]:
        """Return up to ``limit`` snippets most similar to ``query``, with match scores."""
        collection = scope_collection(self._base, tenant)
        if not await self._client.collection_exists(collection):
            return []
        vector = (await self._embed([query]))[0]
        result = await self._client.query_points(
            collection_name=collection, query=vector, limit=limit, with_payload=True
        )
        return [
            RecallHit(
                id=int(point.id),
                session_id=str((point.payload or {}).get("session_id", "")),
                text=str((point.payload or {}).get("text", "")),
                score=float(point.score),
            )
            for point in result.points
            if point.payload
        ]

    async def recall(self, *, tenant: str, query: str, limit: int = 4) -> list[str]:
        """The agent's recall path: just the text of the most-similar snippets."""
        hits = await self.search(tenant=tenant, query=query, limit=limit)
        return [hit.text for hit in hits]

    async def forget_session(self, *, tenant: str, session_id: str) -> None:
        """Drop every indexed snippet belonging to ``session_id``."""
        collection = scope_collection(self._base, tenant)
        if not await self._client.collection_exists(collection):
            return
        await self._client.delete(
            collection_name=collection,
            points_selector=FilterSelector(
                filter=Filter(
                    must=[FieldCondition(key="session_id", match=MatchValue(value=session_id))]
                )
            ),
        )

    async def forget_point(self, *, tenant: str, point_id: int) -> int:
        """Drop a single snippet from recall so it stops surfacing. Returns 1 if removed.

        Only the recall vector is removed — the source message in ``agent_messages`` is left
        intact (forgetting curates cross-chat recall, it does not rewrite the conversation).
        """
        collection = scope_collection(self._base, tenant)
        if not await self._client.collection_exists(collection):
            return 0
        await self._client.delete(
            collection_name=collection, points_selector=PointIdsList(points=[point_id])
        )
        return 1
