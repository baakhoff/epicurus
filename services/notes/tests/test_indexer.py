"""Unit tests for the notes vector indexer (Qdrant + embeddings are faked)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from qdrant_client.models import Distance, VectorParams

from epicurus_notes.indexer import NotesIndexer

TENANT = "test"


class _FakePlatform:
    """Returns a fixed 3-dim vector per input text and records the calls."""

    def __init__(self) -> None:
        self.embedded: list[list[str]] = []

    async def embed(self, texts: list[str], **_: Any) -> list[list[float]]:
        self.embedded.append(texts)
        return [[0.1, 0.2, 0.3] for _ in texts]


def _indexer(qdrant: Any, platform: Any) -> NotesIndexer:
    return NotesIndexer(qdrant, platform, tenant=TENANT)


def test_collection_is_tenant_scoped() -> None:
    idx = _indexer(AsyncMock(), _FakePlatform())
    assert idx.collection == "test__notes"


async def test_index_note_embeds_and_upserts() -> None:
    qdrant = AsyncMock()
    qdrant.collection_exists = AsyncMock(return_value=False)
    platform = _FakePlatform()
    idx = _indexer(qdrant, platform)

    count = await idx.index_note("my-note", "# Title\n\nbody text")

    assert count == qdrant.upsert.call_args.kwargs["points"].__len__()
    assert count >= 1
    # New collection was created with the embedding's dimensionality.
    qdrant.create_collection.assert_awaited_once()
    # Every point carries the note slug in its payload.
    points = qdrant.upsert.call_args.kwargs["points"]
    assert all(p.payload["slug"] == "my-note" for p in points)


async def test_index_empty_note_skips_upsert() -> None:
    qdrant = AsyncMock()
    qdrant.collection_exists = AsyncMock(return_value=False)
    idx = _indexer(qdrant, _FakePlatform())

    count = await idx.index_note("blank", "   \n\n  ")

    assert count == 0
    qdrant.upsert.assert_not_called()


async def test_index_note_deletes_old_vectors_first() -> None:
    qdrant = AsyncMock()
    qdrant.collection_exists = AsyncMock(return_value=True)
    idx = _indexer(qdrant, _FakePlatform())

    await idx.index_note("my-note", "# A\n\nbody")

    # Existing collection → stale vectors for the slug are removed before re-upsert.
    qdrant.delete.assert_awaited()


async def test_delete_note_removes_vectors() -> None:
    qdrant = AsyncMock()
    qdrant.collection_exists = AsyncMock(return_value=True)
    idx = _indexer(qdrant, _FakePlatform())

    await idx.delete_note("my-note")

    qdrant.delete.assert_awaited_once()


async def test_reindex_drops_collection_and_re_embeds_every_note() -> None:
    # The re-embed action (#332): drop the whole collection (vectors are model-specific), then
    # re-embed every note with the current model.
    qdrant = AsyncMock()
    qdrant.collection_exists = AsyncMock(return_value=True)
    platform = _FakePlatform()
    idx = _indexer(qdrant, platform)

    total = await idx.reindex([("a", "# A\n\nbody a"), ("b", "# B\n\nbody b")])

    qdrant.delete_collection.assert_awaited_once()
    assert total >= 2
    assert len(platform.embedded) == 2  # one embed pass per note


# ── Vector-dimension drift (#865) ─────────────────────────────────────────────


class _StatefulQdrant:
    """A Qdrant stand-in that holds a collection's width and rejects a mismatched upsert."""

    def __init__(self, dim: int | None = None) -> None:
        self.dim = dim
        self.created: list[int] = []
        self.dropped = 0
        self.stored: list[list[float]] = []

    async def collection_exists(self, name: str) -> bool:
        return self.dim is not None

    async def get_collection(self, name: str) -> Any:
        assert self.dim is not None  # only called when the collection exists
        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors=VectorParams(size=self.dim, distance=Distance.COSINE)
                )
            )
        )

    async def create_collection(self, name: str, *, vectors_config: VectorParams) -> None:
        self.dim = vectors_config.size
        self.created.append(vectors_config.size)

    async def delete_collection(self, name: str) -> None:
        self.dim = None
        self.dropped += 1
        self.stored.clear()

    async def upsert(self, *, collection_name: str, points: list[Any]) -> None:
        for point in points:
            if len(point.vector) != self.dim:
                raise ValueError(f"Vector dimension error: expected {self.dim}")
            self.stored.append(point.vector)

    async def delete(self, **_: Any) -> None:
        return None


class _WidthPlatform:
    """An embedder whose output width the test controls."""

    def __init__(self, dim: int) -> None:
        self.dim = dim

    async def embed(self, texts: list[str], **_: Any) -> list[list[float]]:
        return [[0.1] * self.dim for _ in texts]


async def test_switching_to_a_wider_embedding_model_recreates_the_collection() -> None:
    # A collection built at 768 cannot take 1536-d vectors — Qdrant rejects the upsert with an
    # opaque dimension error that no retry fixes. Postgres holds the notes, so the collection is
    # recreated at the new width and the write lands; the operator's "Re-embed everything"
    # (#332) then restores the rest.
    qdrant = _StatefulQdrant()
    platform = _WidthPlatform(768)
    idx = _indexer(qdrant, platform)
    await idx.index_note("a", "# A\n\nbody a")
    assert qdrant.dim == 768

    platform.dim = 1536
    count = await idx.index_note("b", "# B\n\nbody b")

    assert count >= 1
    assert qdrant.dim == 1536
    assert qdrant.dropped == 1
    assert all(len(v) == 1536 for v in qdrant.stored)


async def test_an_unchanged_width_never_recreates_the_collection() -> None:
    qdrant = _StatefulQdrant()
    idx = _indexer(qdrant, _WidthPlatform(768))
    await idx.index_note("a", "# A\n\nbody a")
    await idx.index_note("b", "# B\n\nbody b")
    assert qdrant.dropped == 0
    assert qdrant.created == [768]


async def test_reindex_rebuilds_at_the_current_width_without_a_heal() -> None:
    # The documented step after a model switch: reindex drops the collection outright, so the
    # first write recreates it at today's width — no drift to heal.
    qdrant = _StatefulQdrant()
    platform = _WidthPlatform(768)
    idx = _indexer(qdrant, platform)
    await idx.index_note("a", "# A\n\nbody a")

    platform.dim = 1536
    await idx.reindex([("a", "# A\n\nbody a"), ("b", "# B\n\nbody b")])

    assert qdrant.dim == 1536
    assert all(len(v) == 1536 for v in qdrant.stored)
    assert qdrant.dropped == 1  # by reindex itself, not by a drift heal
