"""SemanticRecall against an in-memory Qdrant — index → list/search/count/forget."""

from __future__ import annotations

import hashlib

from qdrant_client import AsyncQdrantClient

from epicurus_core_app.memory.recall import SemanticRecall


def _embed_one(text: str, dim: int = 16) -> list[float]:
    """A deterministic stub embedding: identical text → identical vector (cosine 1.0)."""
    digest = hashlib.sha256(text.encode()).digest()
    return [digest[i % len(digest)] / 255.0 for i in range(dim)]


async def _embed(texts: list[str]) -> list[list[float]]:
    return [_embed_one(text) for text in texts]


def _recall() -> tuple[SemanticRecall, AsyncQdrantClient]:
    client = AsyncQdrantClient(location=":memory:")
    return SemanticRecall(client, _embed), client


async def test_list_points_newest_first_and_count() -> None:
    recall, client = _recall()
    try:
        await recall.index(tenant="t1", session_id="s1", text="first", point_id=1)
        await recall.index(tenant="t1", session_id="s1", text="second", point_id=2)
        await recall.index(tenant="t1", session_id="s2", text="third", point_id=3)
        points = await recall.list_points(tenant="t1")
        assert [p.id for p in points] == [3, 2, 1]  # id ≈ chronological, newest first
        assert points[0].text == "third"
        assert points[0].session_id == "s2"
        assert await recall.count(tenant="t1") == 3
    finally:
        await client.close()


async def test_list_points_respects_limit() -> None:
    recall, client = _recall()
    try:
        for i in range(1, 6):
            await recall.index(tenant="t1", session_id="s1", text=f"m{i}", point_id=i)
        points = await recall.list_points(tenant="t1", limit=2)
        assert [p.id for p in points] == [5, 4]  # newest two
        assert await recall.count(tenant="t1") == 5  # total unaffected by limit
    finally:
        await client.close()


async def test_empty_collection_is_clean() -> None:
    recall, client = _recall()
    try:
        assert await recall.list_points(tenant="t1") == []
        assert await recall.count(tenant="t1") == 0
        assert await recall.search(tenant="t1", query="anything") == []
        assert await recall.recall(tenant="t1", query="anything") == []
    finally:
        await client.close()


async def test_search_ranks_the_best_match_first() -> None:
    recall, client = _recall()
    try:
        await recall.index(tenant="t1", session_id="s1", text="alpha apples", point_id=1)
        await recall.index(tenant="t1", session_id="s1", text="beta bananas", point_id=2)
        hits = await recall.search(tenant="t1", query="alpha apples", limit=2)
        assert hits[0].id == 1
        assert hits[0].text == "alpha apples"
        assert hits[0].score >= hits[-1].score
        # the agent's recall path returns just the text of the same ranking
        assert (await recall.recall(tenant="t1", query="alpha apples", limit=1)) == ["alpha apples"]
    finally:
        await client.close()


async def test_forget_point_removes_one_snippet() -> None:
    recall, client = _recall()
    try:
        await recall.index(tenant="t1", session_id="s1", text="keep", point_id=1)
        await recall.index(tenant="t1", session_id="s1", text="drop", point_id=2)
        assert await recall.forget_point(tenant="t1", point_id=2) == 1
        assert [p.id for p in await recall.list_points(tenant="t1")] == [1]
        assert await recall.count(tenant="t1") == 1
        # forgetting in a tenant with no collection is a no-op
        assert await recall.forget_point(tenant="absent", point_id=99) == 0
    finally:
        await client.close()


async def test_recall_is_tenant_scoped() -> None:
    recall, client = _recall()
    try:
        await recall.index(tenant="t1", session_id="s1", text="one", point_id=1)
        await recall.index(tenant="t2", session_id="s1", text="two", point_id=2)
        assert [p.text for p in await recall.list_points(tenant="t1")] == ["one"]
        assert [p.text for p in await recall.list_points(tenant="t2")] == ["two"]
    finally:
        await client.close()
