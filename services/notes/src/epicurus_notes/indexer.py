"""Vector indexer — keep a note's chunks in the tenant's ``<tenant>__notes`` collection.

Postgres is the source of truth (see :mod:`epicurus_notes.db`); this maintains the
derived embeddings so notes are **RAG-ready in their own collection** (issue #134).
Embeddings are obtained via the core's platform API, so the module never holds a
provider key (ADR-0010).

Deliberately exposes **no search method**: notes are attach-only — the agent has no
retrieval tool over this collection (the boundary that distinguishes Notes from
Knowledge). The collection exists so a future, opt-in retrieval path needs no
re-index; today nothing queries it.
"""

from __future__ import annotations

import uuid

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
from epicurus_notes.chunker import chunk_note

log = get_logger("notes.indexer")

# Fixed UUID5 namespace so chunk point IDs are deterministic across runs.
_CHUNK_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # standard DNS namespace


def _chunk_point_id(slug: str, chunk_index: int) -> str:
    """Deterministic UUID5 point ID for a specific chunk within a note."""
    return str(uuid.uuid5(_CHUNK_NS, f"{slug}:{chunk_index}"))


class NotesIndexer:
    """Maintains one note's chunks in the tenant-scoped Qdrant collection."""

    def __init__(
        self,
        qdrant: AsyncQdrantClient,
        platform: PlatformClient,
        *,
        tenant: str,
        collection_base: str = "notes",
        chunk_max_chars: int = 2000,
    ) -> None:
        self._qdrant = qdrant
        self._platform = platform
        self._tenant = tenant
        self._max_chars = chunk_max_chars
        self._collection = scope_collection(collection_base, tenant)
        self._ensured = False

    @property
    def collection(self) -> str:
        """The tenant-scoped collection name (``<tenant>__notes``)."""
        return self._collection

    async def _ensure_collection(self, dim: int) -> None:
        if self._ensured:
            return
        if not await self._qdrant.collection_exists(self._collection):
            await self._qdrant.create_collection(
                self._collection,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )
        self._ensured = True

    async def _delete_vectors(self, slug: str) -> None:
        """Remove every Qdrant point whose payload ``slug`` matches."""
        if not await self._qdrant.collection_exists(self._collection):
            return
        await self._qdrant.delete(
            collection_name=self._collection,
            points_selector=FilterSelector(
                filter=Filter(must=[FieldCondition(key="slug", match=MatchValue(value=slug))])
            ),
        )

    async def index_note(self, slug: str, content: str) -> int:
        """Re-index one note: drop its old vectors, then chunk, embed, and upsert.

        Returns the number of chunks indexed. An empty note leaves the collection
        with no points for *slug* (and returns 0).
        """
        await self._delete_vectors(slug)
        chunks = chunk_note(content, self._max_chars)
        if not chunks:
            return 0

        vectors = await self._platform.embed([c.text for c in chunks])
        await self._ensure_collection(len(vectors[0]))
        points = [
            PointStruct(
                id=_chunk_point_id(slug, c.index),
                vector=vectors[i],
                payload={
                    "slug": slug,
                    "chunk_index": c.index,
                    "heading": c.heading,
                    "text": c.text,
                },
            )
            for i, c in enumerate(chunks)
        ]
        await self._qdrant.upsert(collection_name=self._collection, points=points)
        log.debug("indexed note", slug=slug, chunks=len(chunks))
        return len(chunks)

    async def delete_note(self, slug: str) -> None:
        """Drop all vectors for a deleted note."""
        await self._delete_vectors(slug)

    async def reindex(self, notes: list[tuple[str, str]]) -> int:
        """Re-embed every note from scratch (#332): drop the whole collection, then re-index
        each ``(slug, content)`` with the current embedding model.

        Used by the core's re-embed fan-out when the embedding model changes — vectors built
        with the old model are incompatible. Returns the total chunks written.
        """
        if await self._qdrant.collection_exists(self._collection):
            await self._qdrant.delete_collection(self._collection)
        total = 0
        for slug, content in notes:
            total += await self.index_note(slug, content)
        return total
