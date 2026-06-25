"""Unit tests for the notes vector indexer (Qdrant + embeddings are faked)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

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
