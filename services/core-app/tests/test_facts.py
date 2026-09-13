"""UserFactStore against an in-memory Qdrant — save (+ dedup) → list/search/count/forget."""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
from typing import Any

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, VectorParams

from epicurus_core_app.memory.facts import (
    SOURCE_AUTO,
    SOURCE_TOOL,
    Embedder,
    EmbeddingDimensionChanged,
    EmbeddingDimensionUnreadable,
    RecallDimensionError,
    UserFactStore,
    vector_size,
)


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


def _slow_embedder(dim: int, *, slow_for: str, delay: float = 0.25) -> Embedder:
    """An embedder pinned to *dim* that stalls only on *slow_for*.

    Lets a test overrun a caller's time-box on the **rebuild** (which embeds the stored fact)
    while the query's own embed stays fast — the shape of the real failure, where the query is
    one short call and the rebuild is the whole corpus.
    """

    async def _embed_slowly(texts: list[str]) -> list[list[float]]:
        if slow_for in texts:
            await asyncio.sleep(delay)
        return [_embed_one(text, dim) for text in texts]

    return _embed_slowly


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


async def test_rebuild_cap_paginates_and_preserves_all_facts_beyond_cap() -> None:
    """A small rebuild_cap bounds page size only — the reconcile must never drop a fact (#450)."""
    client = AsyncQdrantClient(location=":memory:")
    try:
        store = UserFactStore(client, _embedder(16), rebuild_cap=2)
        texts = ("one", "two", "three", "four", "five")
        for text in texts:
            assert await store.save(tenant="t1", text=text) is not None
        assert await store.count(tenant="t1") == 5

        migrated = await store.reembed_all(tenant="t1")
        assert migrated == 5  # every fact survives even though the scroll page size is 2

        assert await store.count(tenant="t1") == 5
        assert {f.text for f in await store.list_facts(tenant="t1")} == set(texts)
    finally:
        await client.close()


# ── #944 / ADR-0141: a dimension change is healed or named, never silent ──────────────────


class _ShapedQdrant:
    """Enough of the async client for ``_ensure``: an existing collection of a given shape."""

    def __init__(self, vectors: Any) -> None:
        self.vectors = vectors
        self.get_collection_calls = 0

    async def collection_exists(self, name: str) -> bool:
        return True

    async def get_collection(self, name: str) -> Any:
        self.get_collection_calls += 1
        return SimpleNamespace(config=SimpleNamespace(params=SimpleNamespace(vectors=self.vectors)))

    async def query_points(self, **_: Any) -> Any:
        return SimpleNamespace(points=[])


class _ScriptedQdrant:
    """A collection that changes width underneath a process that already confirmed it.

    ``confirmed`` is what the reconcile reads (so the store caches it as fine); ``actual`` is
    what the collection really holds once the query has been rejected. That is the residual
    case the error-path mapping exists for — another process healed the collection, or an
    operator recreated it — and the one where Qdrant's raw 400 used to be all anyone saw.
    """

    def __init__(self, *, confirmed: int, actual: int, reject: bool = True) -> None:
        self._confirmed = confirmed
        self._actual = actual
        self._reject = reject
        self.rejected = False

    async def collection_exists(self, name: str) -> bool:
        return True

    async def get_collection(self, name: str) -> Any:
        size = self._actual if self.rejected else self._confirmed
        params = VectorParams(size=size, distance=Distance.COSINE)
        return SimpleNamespace(config=SimpleNamespace(params=SimpleNamespace(vectors=params)))

    async def query_points(self, **_: Any) -> Any:
        if self._reject:
            self.rejected = True
            raise RuntimeError(
                "Unexpected Response: 400 (Bad Request) Raw response content: "
                "Vector dimension error: expected dim: 16, got 8"
            )
        return SimpleNamespace(points=[])


def test_vector_size_reads_every_shape_a_collection_can_report() -> None:
    params = VectorParams(size=768, distance=Distance.COSINE)
    assert vector_size(params) == 768
    # A single-entry mapping is still one unambiguous width — reading it as "unknown" is what
    # let the reconcile skip silently in #944.
    assert vector_size({"text": params}) == 768
    # Several named vectors have no single width to compare; guessing one would drop data.
    assert (
        vector_size({"text": params, "title": VectorParams(size=8, distance=Distance.COSINE)})
        is None
    )
    assert vector_size(None) is None


async def test_an_unreadable_vector_configuration_is_named_and_never_cached() -> None:
    """#944's mechanism: an unrecognised shape used to be a silent no-op that still cached.

    The collection was then marked reconciled for the process's whole life even though nothing
    was fixed, so every later recall repeated the identical fast 400 forever.
    """
    qdrant = _ShapedQdrant(
        {
            "text": VectorParams(size=16, distance=Distance.COSINE),
            "title": VectorParams(size=8, distance=Distance.COSINE),
        }
    )
    store = UserFactStore(qdrant, _embedder(8))  # type: ignore[arg-type]

    with pytest.raises(EmbeddingDimensionUnreadable):
        await store.search(tenant="t1", query="anything")

    state = store.recall_dimension()
    assert state.status == "unreadable"
    assert state.expected_dim == 8
    assert "Re-embed everything" in state.detail

    with pytest.raises(EmbeddingDimensionUnreadable):
        await store.search(tenant="t1", query="anything")
    # Re-read rather than served from a cache that claims a check which never happened.
    assert qdrant.get_collection_calls == 2


async def test_search_names_a_width_change_instead_of_forwarding_qdrants_error() -> None:
    qdrant = _ScriptedQdrant(confirmed=8, actual=16)
    store = UserFactStore(qdrant, _embedder(8))  # type: ignore[arg-type]

    with pytest.raises(EmbeddingDimensionChanged) as raised:
        await store.search(tenant="t1", query="anything")

    message = str(raised.value)
    assert "16→8" in message
    assert "Re-embed everything" in message
    assert "Unexpected Response" not in message  # never the provider's raw text
    assert "t1__facts" not in message  # nor the tenant-scoped collection name
    assert store.recall_dimension().status == "changed"


async def test_an_unrelated_backend_failure_is_forwarded_unchanged() -> None:
    """Only a genuine width mismatch is renamed — Qdrant being down stays Qdrant being down."""
    qdrant = _ScriptedQdrant(confirmed=8, actual=8)
    store = UserFactStore(qdrant, _embedder(8))  # type: ignore[arg-type]

    with pytest.raises(RuntimeError) as raised:
        await store.search(tenant="t1", query="anything")

    assert not isinstance(raised.value, RecallDimensionError)
    assert store.recall_dimension().status == "ok"


async def test_a_heal_is_reported_to_the_models_page() -> None:
    client = AsyncQdrantClient(location=":memory:")
    try:
        old_store = UserFactStore(client, _embedder(16))
        await old_store.save(tenant="t1", text="Prefers dark mode")

        new_store = UserFactStore(client, _embedder(8))
        assert new_store.recall_dimension().status == "ok"
        await new_store.search(tenant="t1", query="Prefers dark mode")

        state = new_store.recall_dimension()
        assert state.status == "healed"
        assert (state.stored_dim, state.expected_dim) == (16, 8)
        assert "16-d to 8-d" in state.detail

        # Running the documented cure clears what this process had observed.
        await new_store.reembed_all(tenant="t1")
        assert new_store.recall_dimension().status == "ok"
    finally:
        await client.close()


async def test_a_recall_that_gives_up_on_its_budget_still_finishes_the_rebuild() -> None:
    """The heal must outlive the caller's time-box (ADR-0141).

    Recall is time-boxed (``MEMORY_RECALL_TIMEOUT_S``), and a rebuild of a real fact corpus
    through a hosted embedder can take longer than that budget. Awaited unshielded, every turn
    would start the heal, be cancelled, and start it again — a livelock in which recall is
    dead forever — and a cancellation landing between the collection's drop and its refill
    would take the operator's whole fact corpus with it.
    """
    client = AsyncQdrantClient(location=":memory:")
    try:
        old_store = UserFactStore(client, _embedder(16))
        await old_store.save(tenant="t1", text="Prefers dark mode")

        new_store = UserFactStore(client, _slow_embedder(8, slow_for="Prefers dark mode"))
        with pytest.raises(TimeoutError):
            # The query text embeds fast; the *rebuild* of the stored fact is what overruns.
            await asyncio.wait_for(new_store.search(tenant="t1", query="dark mode please"), 0.05)

        for _ in range(200):  # the shielded rebuild finishes on its own
            if new_store.recall_dimension().status == "healed":
                break
            await asyncio.sleep(0.02)

        assert new_store.recall_dimension().status == "healed"
        facts = await new_store.list_facts(tenant="t1")
        assert [f.text for f in facts] == ["Prefers dark mode"]  # nothing was lost
        info = await client.get_collection("t1__facts")
        assert isinstance(info.config.params.vectors, VectorParams)
        assert info.config.params.vectors.size == 8
        # And the next recall simply works, instead of restarting a doomed heal.
        assert await new_store.recall(tenant="t1", query="dark mode please") == [
            "Prefers dark mode"
        ]
    finally:
        await client.close()
