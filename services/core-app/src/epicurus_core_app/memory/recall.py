"""Semantic recall — embed conversation text and retrieve it from Qdrant (tenant-scoped)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from epicurus_core.tenancy import scope_collection

Embedder = Callable[[list[str]], Awaitable[list[list[float]]]]


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

    async def recall(self, *, tenant: str, query: str, limit: int = 4) -> list[str]:
        """Return up to ``limit`` snippets most similar to ``query``."""
        collection = scope_collection(self._base, tenant)
        if not await self._client.collection_exists(collection):
            return []
        vector = (await self._embed([query]))[0]
        result = await self._client.query_points(
            collection_name=collection, query=vector, limit=limit
        )
        return [str(point.payload["text"]) for point in result.points if point.payload]
