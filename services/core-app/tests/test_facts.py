"""UserFactStore against an in-memory Qdrant — save (+ dedup) → list/search/count/forget."""

from __future__ import annotations

import asyncio
import hashlib

from qdrant_client import AsyncQdrantClient

from epicurus_core_app.memory.facts import SOURCE_AUTO, SOURCE_TOOL, UserFactStore


def _embed_one(text: str, dim: int = 16) -> list[float]:
    """A deterministic stub embedding: identical text → identical vector (cosine 1.0)."""
    digest = hashlib.sha256(text.encode()).digest()
    return [digest[i % len(digest)] / 255.0 for i in range(dim)]


async def _embed(texts: list[str]) -> list[list[float]]:
    return [_embed_one(text) for text in texts]


def _store() -> tuple[UserFactStore, AsyncQdrantClient]:
    client = AsyncQdrantClient(location=":memory:")
    return UserFactStore(client, _embed), client


async def test_save_then_list_newest_first_and_count() -> None:
    store, client = _store()
    try:
        await store.save(tenant="t1", text="Lives in Belgrade", source=SOURCE_AUTO)
        await asyncio.sleep(0.005)  # guarantee a strictly later created_at
        await store.save(tenant="t1", text="Prefers metric units", source=SOURCE_TOOL)
        facts = await store.list_facts(tenant="t1")
        assert [f.text for f in facts] == ["Prefers metric units", "Lives in Belgrade"]
        assert facts[0].source == SOURCE_TOOL
        assert facts[0].created_at is not None
        assert await store.count(tenant="t1") == 2
    finally:
        await client.close()


async def test_save_dedups_a_near_identical_fact() -> None:
    store, client = _store()
    try:
        first = await store.save(tenant="t1", text="Prefers dark mode")
        dup = await store.save(tenant="t1", text="Prefers dark mode")  # identical → dropped
        assert first is not None
        assert dup is None
        assert await store.count(tenant="t1") == 1
    finally:
        await client.close()


async def test_save_keeps_distinct_facts() -> None:
    store, client = _store()
    try:
        a = await store.save(tenant="t1", text="Prefers dark mode")
        b = await store.save(tenant="t1", text="Works on a project called epicurus")
        assert a is not None and b is not None
        assert await store.count(tenant="t1") == 2
    finally:
        await client.close()


async def test_save_ignores_blank_text() -> None:
    store, client = _store()
    try:
        assert await store.save(tenant="t1", text="   ") is None
        assert await store.count(tenant="t1") == 0
    finally:
        await client.close()


async def test_search_ranks_the_best_match_first() -> None:
    store, client = _store()
    try:
        await store.save(tenant="t1", text="alpha apples")
        await store.save(tenant="t1", text="beta bananas")
        hits = await store.search(tenant="t1", query="alpha apples", limit=2)
        assert hits[0].text == "alpha apples"
        assert hits[0].score >= hits[-1].score
        assert (await store.recall(tenant="t1", query="alpha apples", limit=1)) == ["alpha apples"]
    finally:
        await client.close()


async def test_forget_removes_one_fact() -> None:
    store, client = _store()
    try:
        keep = await store.save(tenant="t1", text="keep me")
        drop = await store.save(tenant="t1", text="forget me")
        assert keep is not None and drop is not None
        assert await store.forget(tenant="t1", fact_id=drop.id) == 1
        assert [f.text for f in await store.list_facts(tenant="t1")] == ["keep me"]
        assert await store.count(tenant="t1") == 1
        # forgetting in a tenant with no collection is a no-op
        assert await store.forget(tenant="absent", fact_id="whatever") == 0
    finally:
        await client.close()


async def test_empty_collection_is_clean() -> None:
    store, client = _store()
    try:
        assert await store.list_facts(tenant="t1") == []
        assert await store.count(tenant="t1") == 0
        assert await store.search(tenant="t1", query="anything") == []
        assert await store.recall(tenant="t1", query="anything") == []
    finally:
        await client.close()


async def test_facts_are_tenant_scoped() -> None:
    store, client = _store()
    try:
        await store.save(tenant="t1", text="one")
        await store.save(tenant="t2", text="two")
        assert [f.text for f in await store.list_facts(tenant="t1")] == ["one"]
        assert [f.text for f in await store.list_facts(tenant="t2")] == ["two"]
    finally:
        await client.close()
