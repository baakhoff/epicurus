"""Vector-dimension drift: switching the embedding model must not break indexing (#865).

The fake Qdrant here is stateful and *enforces* the dimension rule — an upsert of vectors
whose width differs from the collection's raises, exactly as the real server does. Without
that, every test in this file would pass against the broken behaviour it exists to pin.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from qdrant_client.models import Distance, VectorParams
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_knowledge.db import DocIndex, NoteIndex
from epicurus_knowledge.dimensions import (
    CollectionDimensionGuard,
    EmbeddingDimensionChanged,
    EmbeddingDimensionMismatch,
)
from epicurus_knowledge.indexer import KnowledgeIndexer
from epicurus_knowledge.module_docs import ModuleDocLedger, ModuleDocsIndexer

TENANT = "test"


class _FakeQdrant:
    """Enough of the async client to hold points, and to reject a width mismatch."""

    def __init__(self) -> None:
        self.dims: dict[str, int] = {}
        # collection -> point id -> (vector, payload)
        self.points: dict[str, dict[str, tuple[list[float], dict[str, Any]]]] = {}
        self.created: list[tuple[str, int]] = []
        self.dropped: list[str] = []
        # The shape get_collection reports for ``config.params.vectors``:
        #   "plain"     — one unnamed VectorParams (what we create, and the usual answer)
        #   "one_named" — a single-entry mapping: still one unambiguous width (#944)
        #   "multi"     — several named vectors: no single width to compare, hands off
        self.vectors_shape = "plain"
        self.get_collection_calls = 0
        # Make query_points fail the way the real server fails a width mismatch.
        self.reject_queries = False

    async def collection_exists(self, name: str) -> bool:
        return name in self.dims

    async def get_collection(self, name: str) -> Any:
        self.get_collection_calls += 1
        params = VectorParams(size=self.dims[name], distance=Distance.COSINE)
        vectors: Any = params
        if self.vectors_shape == "one_named":
            vectors = {"text": params}
        elif self.vectors_shape == "multi":
            vectors = {"text": params, "title": VectorParams(size=8, distance=Distance.COSINE)}
        return SimpleNamespace(config=SimpleNamespace(params=SimpleNamespace(vectors=vectors)))

    async def create_collection(self, name: str, *, vectors_config: VectorParams) -> None:
        self.dims[name] = vectors_config.size
        self.points.setdefault(name, {})
        self.created.append((name, vectors_config.size))

    async def delete_collection(self, name: str) -> None:
        self.dims.pop(name, None)
        self.points.pop(name, None)
        self.dropped.append(name)

    async def upsert(self, *, collection_name: str, points: list[Any]) -> None:
        dim = self.dims[collection_name]
        for point in points:
            if len(point.vector) != dim:
                raise ValueError(
                    f"Vector dimension error: expected dim: {dim}, got {len(point.vector)}"
                )
            self.points.setdefault(collection_name, {})[point.id] = (point.vector, point.payload)

    async def delete(self, *, collection_name: str, points_selector: Any) -> None:
        condition = points_selector.filter.must[0]
        key, value = condition.key, condition.match.value
        held = self.points.get(collection_name, {})
        for point_id in [i for i, (_, p) in held.items() if p.get(key) == value]:
            del held[point_id]

    async def query_points(self, **_: Any) -> Any:
        if self.reject_queries:
            # What the real server answers a width mismatch with: opaque, and unfixable by
            # any retry. Search must translate it, not forward it (#879).
            raise RuntimeError(
                "Unexpected Response: 400 (Bad Request) Raw response content: "
                "Vector dimension error: expected dim: 4, got 8"
            )
        return SimpleNamespace(points=[])

    def widths(self, collection: str) -> set[int]:
        """The distinct vector widths currently stored in *collection*."""
        return {len(vector) for vector, _ in self.points.get(collection, {}).values()}


class _FakePlatform:
    """An embedder whose output width the test can change mid-flight."""

    def __init__(self, dim: int = 4) -> None:
        self.dim = dim
        self.calls = 0

    async def embed(self, texts: list[str], **_: Any) -> list[list[float]]:
        self.calls += 1
        return [[0.1] * self.dim for _ in texts]

    async def get_module_model(self, _slot: str) -> str | None:
        return None


@pytest.fixture
async def note_index() -> NoteIndex:
    idx = NoteIndex(create_async_engine("sqlite+aiosqlite:///:memory:"))
    await idx.init()
    return idx


@pytest.fixture
async def doc_index() -> DocIndex:
    idx = DocIndex(create_async_engine("sqlite+aiosqlite:///:memory:"))
    await idx.init()
    return idx


@pytest.fixture
async def module_ledger() -> ModuleDocLedger:
    ledger = ModuleDocLedger(create_async_engine("sqlite+aiosqlite:///:memory:"))
    await ledger.init()
    return ledger


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    (tmp_path / "a.md").write_text("# A\n\nbody a")
    (tmp_path / "b.md").write_text("# B\n\nbody b")
    return tmp_path


# ── The guard itself ──────────────────────────────────────────────────────────


async def test_guard_creates_a_missing_collection_at_the_embedders_width() -> None:
    qdrant = _FakeQdrant()
    guard = CollectionDimensionGuard(qdrant, "test__notes")  # type: ignore[arg-type]
    await guard.ensure(768)
    assert qdrant.dims["test__notes"] == 768
    # A matching width later is a no-op — no drop, no second create.
    await guard.ensure(768)
    assert qdrant.created == [("test__notes", 768)]
    assert qdrant.dropped == []


async def test_guard_recreates_on_a_width_change_and_clears_every_registered_ledger() -> None:
    qdrant = _FakeQdrant()
    guard = CollectionDimensionGuard(qdrant, "test__docs")  # type: ignore[arg-type]
    cleared: list[str] = []
    guard.register_reset(lambda: _record(cleared, "bundled"))
    guard.register_reset(lambda: _record(cleared, "module"))
    await guard.ensure(768)

    with pytest.raises(EmbeddingDimensionChanged):
        await guard.ensure(1536)

    assert qdrant.dims["test__docs"] == 1536
    assert qdrant.dropped == ["test__docs"]
    # *Every* source claiming the shared collection is reset, not just the one that noticed.
    assert cleared == ["bundled", "module"]


async def _record(sink: list[str], name: str) -> None:
    sink.append(name)


async def test_guard_leaves_an_unrecognised_vector_config_alone() -> None:
    # Several named vectors: there is no single width to compare, so the guard must not guess
    # one and drop somebody else's data on the guess.
    qdrant = _FakeQdrant()
    guard = CollectionDimensionGuard(qdrant, "test__docs")  # type: ignore[arg-type]
    await guard.ensure(768)
    qdrant.vectors_shape = "multi"
    guard.forget()

    await guard.ensure(1536)  # no raise

    assert qdrant.dropped == []


async def test_guard_never_caches_a_width_it_could_not_read() -> None:
    """#944's mechanism, in the knowledge twin: unconfirmed must not read as confirmed.

    Caching the *requested* width after failing to read the *actual* one claims a check that
    never happened, so every later pass skips it for the process's lifetime while every write
    keeps failing. The guard must re-read instead.
    """
    qdrant = _FakeQdrant()
    guard = CollectionDimensionGuard(qdrant, "test__docs")  # type: ignore[arg-type]
    await guard.ensure(768)
    qdrant.vectors_shape = "multi"
    guard.forget()

    await guard.ensure(1536)
    first = qdrant.get_collection_calls
    await guard.ensure(1536)

    assert qdrant.get_collection_calls == first + 1  # re-read, not served from a false cache
    assert qdrant.dropped == []


async def test_guard_reads_a_single_named_vector_as_one_width() -> None:
    """A one-entry mapping is still one unambiguous width — reading it as "unknown" is the
    shape that let the core's twin skip its reconcile in silence (#944)."""
    qdrant = _FakeQdrant()
    guard = CollectionDimensionGuard(qdrant, "test__docs")  # type: ignore[arg-type]
    await guard.ensure(768)
    qdrant.vectors_shape = "one_named"
    guard.forget()

    with pytest.raises(EmbeddingDimensionChanged):
        await guard.ensure(1536)

    assert qdrant.dims["test__docs"] == 1536


# ── The vault / bundled-docs indexer ──────────────────────────────────────────


def _indexer(
    note_index: NoteIndex,
    qdrant: _FakeQdrant,
    platform: _FakePlatform,
    vault: Path,
    **kwargs: Any,
) -> KnowledgeIndexer:
    return KnowledgeIndexer(
        note_index,
        qdrant,  # type: ignore[arg-type]
        platform,  # type: ignore[arg-type]
        vault_path=vault,
        tenant=TENANT,
        **kwargs,
    )


async def test_a_widened_embedding_model_rebuilds_the_whole_collection_in_one_pass(
    note_index: NoteIndex, vault: Path
) -> None:
    # The contract: a collection built at 768 and then indexed with a 1536-d model is recreated
    # at 1536 and re-embedded from source — in this one call — rather than failing the upsert
    # with an opaque Qdrant error or half-filling itself.
    qdrant, platform = _FakeQdrant(), _FakePlatform(dim=768)
    indexer = _indexer(note_index, qdrant, platform, vault)
    await indexer.run()
    collection = indexer._collection
    assert qdrant.dims[collection] == 768
    assert qdrant.widths(collection) == {768}
    assert await note_index.count(tenant=TENANT) == 2

    platform.dim = 1536
    (vault / "a.md").write_text("# A\n\nbody a, edited")
    result = await indexer.run()

    assert qdrant.dims[collection] == 1536
    # Every note is present at the new width — including `b.md`, which this pass would have
    # skipped as unchanged had the heal not cleared the ledger and re-walked.
    assert qdrant.widths(collection) == {1536}
    assert result["indexed"] == 2
    assert await note_index.count(tenant=TENANT) == 2


async def test_an_unchanged_embedding_model_never_drops_the_collection(
    note_index: NoteIndex, vault: Path
) -> None:
    qdrant, platform = _FakeQdrant(), _FakePlatform(dim=768)
    indexer = _indexer(note_index, qdrant, platform, vault)
    await indexer.run()
    (vault / "a.md").write_text("# A\n\nbody a, edited")
    await indexer.run()
    assert qdrant.dropped == []


async def test_single_file_reindex_survives_a_width_change(
    note_index: NoteIndex, vault: Path
) -> None:
    # The editor-save path (#130) writes one file. It must not be the one call that reports an
    # opaque Qdrant error after a model switch.
    qdrant, platform = _FakeQdrant(), _FakePlatform(dim=768)
    indexer = _indexer(note_index, qdrant, platform, vault)
    await indexer.run()

    platform.dim = 1536
    chunks = await indexer.index_path("a.md")

    assert chunks >= 1
    assert qdrant.dims[indexer._collection] == 1536
    assert qdrant.widths(indexer._collection) == {1536}
    # The ledger was cleared by the heal, then re-stamped for the file just written — so the
    # next full run re-embeds everything else rather than skipping it as unchanged.
    assert await note_index.list_paths(tenant=TENANT) == ["a.md"]


async def test_reset_makes_the_next_pass_create_at_the_current_width(
    note_index: NoteIndex, vault: Path
) -> None:
    # "Re-embed everything" (#332) is the documented step after switching models: it drops the
    # collection outright, so the next pass creates a fresh one — no heal needed.
    qdrant, platform = _FakeQdrant(), _FakePlatform(dim=768)
    indexer = _indexer(note_index, qdrant, platform, vault)
    await indexer.run()

    platform.dim = 1536
    await indexer.reset()
    await indexer.run()

    assert qdrant.dims[indexer._collection] == 1536
    assert qdrant.widths(indexer._collection) == {1536}
    # Dropped once by reset, never by a heal.
    assert qdrant.dropped == [indexer._collection]


# ── Interaction with the mass de-index fuse (#848) ────────────────────────────


async def test_a_fuse_refusal_still_precedes_any_dimension_heal(
    note_index: NoteIndex, vault: Path
) -> None:
    # The fuse is weighed before a single embedding is requested, so a stale mount can never
    # reach the heal: the collection keeps its old width and its points, and nothing is cleared.
    qdrant, platform = _FakeQdrant(), _FakePlatform(dim=768)
    indexer = _indexer(note_index, qdrant, platform, vault)
    await indexer.run()

    platform.dim = 1536
    for path in vault.glob("*.md"):  # the source "vanishes" — the stale-mount signature
        path.unlink()
    result = await indexer.run()

    assert result["fuse_tripped"] == 1
    assert indexer.fuse.tripped
    assert qdrant.dims[indexer._collection] == 768
    assert qdrant.widths(indexer._collection) == {768}
    assert await note_index.count(tenant=TENANT) == 2


async def test_a_dimension_heal_does_not_trip_the_fuse(note_index: NoteIndex, vault: Path) -> None:
    # The heal clears the ledger itself; it is not a de-index the fuse should weigh. The
    # re-walk then sees an empty ledger, which never trips (nothing to protect).
    qdrant, platform = _FakeQdrant(), _FakePlatform(dim=768)
    indexer = _indexer(note_index, qdrant, platform, vault)
    await indexer.run()

    platform.dim = 1536
    (vault / "a.md").write_text("# A\n\nbody a, edited")
    result = await indexer.run()

    assert result["fuse_tripped"] == 0
    assert not indexer.fuse.tripped


# ── The shared <tenant>__docs collection ──────────────────────────────────────


def _module_docs_indexer(
    ledger: ModuleDocLedger,
    qdrant: _FakeQdrant,
    platform: Any,
    guard: CollectionDimensionGuard,
) -> ModuleDocsIndexer:
    return ModuleDocsIndexer(
        ledger,
        qdrant,  # type: ignore[arg-type]
        platform,
        tenant=TENANT,
        dimensions=guard,
    )


def _module_platform(dim_holder: _FakePlatform) -> Any:
    """A platform mock serving one module with one doc, embedding at *dim_holder*'s width."""
    platform = MagicMock()
    platform.list_modules = AsyncMock(
        return_value=[
            {"enabled": True, "removed": False, "manifest": {"name": "echo", "docs_url": "/d"}}
        ]
    )
    platform.get_module_docs = AsyncMock(
        return_value=[{"path": "readme.md", "content": "# Echo\n\nhello"}]
    )
    platform.get_module_model = AsyncMock(return_value=None)
    platform.embed = AsyncMock(side_effect=lambda texts, **_: [[0.1] * dim_holder.dim] * len(texts))
    return platform


async def test_healing_the_shared_docs_collection_clears_both_ledgers(
    doc_index: DocIndex, module_ledger: ModuleDocLedger, tmp_path: Path
) -> None:
    # The bundled platform docs and the per-module docs share <tenant>__docs. A recreate driven
    # by either drops the other's points too, so both ledgers must be cleared — otherwise the
    # survivor keeps claiming vectors that no longer exist, and never re-embeds them.
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "guide.md").write_text("# Guide\n\nbundled body")

    qdrant, platform = _FakeQdrant(), _FakePlatform(dim=768)
    guard = CollectionDimensionGuard(qdrant, f"{TENANT}__docs")  # type: ignore[arg-type]
    bundled = KnowledgeIndexer(
        doc_index,
        qdrant,  # type: ignore[arg-type]
        platform,  # type: ignore[arg-type]
        vault_path=docs_dir,
        tenant=TENANT,
        collection_base="docs",
        dimensions=guard,
    )
    module_docs = _module_docs_indexer(module_ledger, qdrant, _module_platform(platform), guard)

    await bundled.run()
    await module_docs.run()
    assert await doc_index.count(tenant=TENANT) == 1
    assert await module_ledger.count(tenant=TENANT) == 1
    assert qdrant.dims[f"{TENANT}__docs"] == 768

    # The module-docs sync is the one that notices the new width.
    platform.dim = 1536
    await module_docs.run()

    assert qdrant.dims[f"{TENANT}__docs"] == 1536
    assert qdrant.widths(f"{TENANT}__docs") == {1536}
    # Its own corpus was rebuilt in that same call...
    assert await module_ledger.count(tenant=TENANT) == 1
    # ...and the bundled-docs ledger was cleared, so its next pass re-embeds rather than
    # skipping every file as unchanged against vectors that are gone.
    assert await doc_index.count(tenant=TENANT) == 0

    await bundled.run()
    assert await doc_index.count(tenant=TENANT) == 1
    assert qdrant.widths(f"{TENANT}__docs") == {1536}


# ── Search: name the change, never rebuild (#879) ─────────────────────────────


async def test_search_names_a_width_change_instead_of_forwarding_qdrants_error(
    note_index: NoteIndex, vault: Path
) -> None:
    """A query embedded at the new width must answer with the cause and the cure.

    Search is where a switched embedding model is noticed first, and Qdrant's own answer is an
    opaque ``Vector dimension error`` that reads like a backend fault. The agent sees the tool's
    error text, so that text has to be the one the operator can act on.
    """
    qdrant, platform = _FakeQdrant(), _FakePlatform(dim=4)
    indexer = _indexer(note_index, qdrant, platform, vault)
    await indexer.run()
    assert qdrant.dims[indexer._collection] == 4

    platform.dim = 8  # the operator switched the embedding model; the index is still 4-d
    qdrant.reject_queries = True

    with pytest.raises(EmbeddingDimensionMismatch) as raised:
        await indexer.search("anything")

    message = str(raised.value)
    assert "4-d" in message and "8-d" in message
    assert "Re-embed everything" in message
    assert "Unexpected Response" not in message  # never the provider's raw text
    # Search never rebuilds — that is the indexer's job, and a rebuild here would silently
    # drop every vector the operator is still searching.
    assert qdrant.dropped == []
    assert qdrant.dims[indexer._collection] == 4


async def test_search_forwards_an_unrelated_backend_failure_unchanged(
    note_index: NoteIndex, vault: Path
) -> None:
    """Only a genuine width mismatch is renamed — Qdrant being down stays Qdrant being down."""
    qdrant, platform = _FakeQdrant(), _FakePlatform(dim=4)
    indexer = _indexer(note_index, qdrant, platform, vault)
    await indexer.run()
    qdrant.reject_queries = True  # the widths still agree (4 == 4)

    with pytest.raises(RuntimeError) as raised:
        await indexer.search("anything")

    assert not isinstance(raised.value, EmbeddingDimensionMismatch)
