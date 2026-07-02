"""UserFactStore against an in-memory Qdrant — save (+ dedup) → list/search/count/forget."""

from __future__ import annotations

import asyncio
import hashlib

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, VectorParams

from epicurus_core_app.memory.facts import SOURCE_AUTO, SOURCE_TOOL, Embedder, UserFactStore


def _embed_one(text: str, dim: int = 16) -> list[float]:
    """A deterministic stub embedding: identical text → identical vector (cosine 1.0)."""
    digest = hashlib.sha256(text.encode()).digest()
    return [digest[i % len(digest)] / 255.0 for i in range(dim)]


async def _embed(texts: list[str]) -> list[list[float]]:
    return [_embed_one(text) for text in texts]


def _embedder(dim: int) -> Embedder:
    """An embedder pinned to *dim* — stands in for a differently-sized model (#436)."""

    async def _embed_at_dim(texts: list[str]) -> list[list[float]]:
        return [_embed_one(text, dim) for text in texts]

    return _embed_at_dim


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


# ── #436: embedding-model dimension drift ────────────────────────────────────────────────


async def test_save_reconciles_a_dimension_drifted_collection() -> None:
    """A collection created under one embedder must not break saves after a model swap."""
    client = AsyncQdrantClient(location=":memory:")
    try:
        old_store = UserFactStore(client, _embedder(16))
        first = await old_store.save(tenant="t1", text="Lives in Belgrade")
        assert first is not None

        new_store = UserFactStore(client, _embedder(8))  # simulates swapping to a smaller model
        second = await new_store.save(tenant="t1", text="Prefers metric units")
        assert second is not None

        facts = await new_store.list_facts(tenant="t1")
        assert {f.text for f in facts} == {"Lives in Belgrade", "Prefers metric units"}
        info = await client.get_collection("t1__facts")
        assert isinstance(info.config.params.vectors, VectorParams)
        assert info.config.params.vectors.size == 8
    finally:
        await client.close()


async def test_search_and_recall_heal_dimension_drift_instead_of_erroring() -> None:
    """The reported symptom (#436): recall must self-heal, not silently return nothing."""
    client = AsyncQdrantClient(location=":memory:")
    try:
        old_store = UserFactStore(client, _embedder(16))
        await old_store.save(tenant="t1", text="Prefers dark mode")

        new_store = UserFactStore(client, _embedder(8))
        hits = await new_store.search(tenant="t1", query="Prefers dark mode")
        assert [h.text for h in hits] == ["Prefers dark mode"]
        assert await new_store.recall(tenant="t1", query="Prefers dark mode") == [
            "Prefers dark mode"
        ]
    finally:
        await client.close()


async def test_ensure_recreates_an_empty_drifted_collection_at_the_new_dim() -> None:
    """Even with zero facts to preserve, a known target dim must still be enforced."""
    client = AsyncQdrantClient(location=":memory:")
    try:
        await client.create_collection(
            "t1__facts", vectors_config=VectorParams(size=16, distance=Distance.COSINE)
        )
        store = UserFactStore(client, _embedder(8))
        saved = await store.save(tenant="t1", text="fresh fact")
        assert saved is not None
        info = await client.get_collection("t1__facts")
        assert isinstance(info.config.params.vectors, VectorParams)
        assert info.config.params.vectors.size == 8
    finally:
        await client.close()


async def test_reembed_all_refreshes_facts_preserving_id_and_metadata() -> None:
    client = AsyncQdrantClient(location=":memory:")
    try:
        store = UserFactStore(client, _embedder(16))
        saved = await store.save(tenant="t1", text="Works on epicurus", source=SOURCE_TOOL)
        assert saved is not None

        migrated = await store.reembed_all(tenant="t1")
        assert migrated == 1

        facts = await store.list_facts(tenant="t1")
        assert len(facts) == 1
        assert facts[0].id == saved.id
        assert facts[0].text == "Works on epicurus"
        assert facts[0].source == SOURCE_TOOL
        assert facts[0].created_at == saved.created_at
    finally:
        await client.close()


async def test_reembed_all_on_a_tenant_with_no_facts_is_a_noop() -> None:
    client = AsyncQdrantClient(location=":memory:")
    try:
        store = UserFactStore(client, _embedder(16))
        assert await store.reembed_all(tenant="absent") == 0
    finally:
        await client.close()


async def test_rebuild_cap_still_migrates_partially_instead_of_crashing() -> None:
    """Hitting the scan cap during a reconcile is a logged, bounded degrade — never a crash."""
    client = AsyncQdrantClient(location=":memory:")
    try:
        store = UserFactStore(client, _embedder(16), rebuild_cap=2)
        for text in ("one", "two", "three"):
            assert await store.save(tenant="t1", text=text) is not None
        assert await store.count(tenant="t1") == 3

        migrated = await store.reembed_all(tenant="t1")
        assert migrated == 2  # the capped scroll only sees 2 of the 3 stored facts

        assert await store.count(tenant="t1") == 2
    finally:
        await client.close()
