"""The mass de-index fuse (#848) — an empty-reading source must not purge the index.

Reconstructs the 2026-08-30 incident in miniature: a vault that reads empty (a stale mount,
not a deletion) while the ledger is full, and the index passes that used to reconcile it to
nothing. Every test drives the real :class:`KnowledgeIndexer` over a tmp-path vault with a
fake Qdrant/platform pair, so what is asserted is the shipped behaviour, not the policy in
isolation — the policy's own edge cases get their own unit tests at the top.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from qdrant_client.models import Distance, VectorParams
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_knowledge.db import NoteIndex
from epicurus_knowledge.fuse import FusePolicy, FuseTrip, IndexFuse, rebuild_refusals
from epicurus_knowledge.indexer import KnowledgeIndexer

TENANT = "test"


# ── The policy in isolation ───────────────────────────────────────────────────


def _fuse(**kwargs: Any) -> IndexFuse:
    return IndexFuse(tenant=TENANT, source="knowledge", policy=FusePolicy(**kwargs))


def test_first_index_never_trips() -> None:
    """An empty ledger has nothing to protect — the very first pass must always run."""
    assert _fuse().evaluate(ledger_rows=0, would_delete=0, source_entries=0) is None


def test_empty_source_with_a_populated_ledger_always_trips() -> None:
    trip = _fuse().evaluate(ledger_rows=40, would_delete=40, source_entries=0)
    assert trip is not None
    assert trip.tenant == TENANT and trip.source == "knowledge"
    assert trip.would_delete == 40
    assert "read empty" in trip.reason


def test_empty_source_trips_below_the_absolute_floor_too() -> None:
    """The floor guards the *ratio* rule only: total disappearance is suspect at any size."""
    trip = _fuse(min_deletions=5).evaluate(ledger_rows=1, would_delete=1, source_entries=0)
    assert trip is not None
    assert "1 entry is indexed" in trip.reason


def test_deletion_above_the_ratio_trips() -> None:
    trip = _fuse().evaluate(ledger_rows=20, would_delete=10, source_entries=10)
    assert trip is not None
    assert "10 of 20" in trip.reason


def test_deletion_below_the_ratio_does_not_trip() -> None:
    assert _fuse().evaluate(ledger_rows=20, would_delete=9, source_entries=11) is None


def test_small_deletion_below_the_floor_does_not_trip() -> None:
    """Two notes of three is 67% — but four deletions are ordinary editing, not a wipe."""
    assert _fuse(min_deletions=5).evaluate(ledger_rows=3, would_delete=2, source_entries=1) is None


def test_force_bypasses_the_fuse() -> None:
    assert _fuse().evaluate(ledger_rows=40, would_delete=40, source_entries=0, force=True) is None


def test_disabled_policy_never_trips() -> None:
    assert _fuse(enabled=False).evaluate(ledger_rows=40, would_delete=40, source_entries=0) is None


def test_trip_and_clear_toggle_the_observable_state() -> None:
    fuse = _fuse()
    assert fuse.tripped is False
    trip = fuse.evaluate(ledger_rows=9, would_delete=9, source_entries=0)
    assert trip is not None
    fuse.trip(trip)
    assert fuse.tripped is True
    assert fuse.last_trip is not None
    assert "knowledge:" in fuse.last_trip.summary()
    fuse.clear()
    assert fuse.tripped is False
    assert fuse.last_trip is None


# ── The indexer, over a real vault ────────────────────────────────────────────

EMBED_DIM = 4


def _fake_vectors(texts: list[str]) -> list[list[float]]:
    return [[float(i), 0.0, 0.0, 0.0] for i in range(len(texts))]


def _platform() -> Any:
    platform = MagicMock()
    platform.embed = AsyncMock(side_effect=lambda texts, **_: _fake_vectors(texts))
    platform.get_module_model = AsyncMock(return_value=None)
    return platform


def _collection_info(dim: int) -> Any:
    """A ``get_collection`` response carrying one unnamed vector config of size *dim*."""
    return SimpleNamespace(
        config=SimpleNamespace(
            params=SimpleNamespace(vectors=VectorParams(size=dim, distance=Distance.COSINE))
        )
    )


def _qdrant() -> Any:
    qdrant = MagicMock()
    qdrant.collection_exists = AsyncMock(return_value=True)
    # Matches the fake embedder's width, so the dimension guard (#865) never heals here.
    qdrant.get_collection = AsyncMock(return_value=_collection_info(EMBED_DIM))
    qdrant.create_collection = AsyncMock()
    qdrant.upsert = AsyncMock()
    qdrant.delete = AsyncMock()
    qdrant.delete_collection = AsyncMock()
    return qdrant


@pytest.fixture
async def note_index() -> NoteIndex:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    idx = NoteIndex(engine)
    await idx.init()
    return idx


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A ten-note knowledge base — enough to clear the fuse's absolute floor."""
    for n in range(10):
        (tmp_path / f"note_{n}.md").write_text(f"# Note {n}\n\nBody {n}.")
    return tmp_path


def _indexer(note_index: NoteIndex, vault: Path, **policy: Any) -> KnowledgeIndexer:
    return KnowledgeIndexer(
        note_index,
        _qdrant(),
        _platform(),
        vault_path=vault,
        tenant=TENANT,
        fuse_policy=FusePolicy(**policy) if policy else None,
    )


def _empty_the_mount(vault: Path) -> None:
    """What a stale bind mount looks like: the directory is there, the notes are not."""
    for note in vault.glob("*.md"):
        note.unlink()


async def test_run_refuses_when_the_vault_reads_empty(note_index: NoteIndex, vault: Path) -> None:
    indexer = _indexer(note_index, vault)
    await indexer.run()
    assert await note_index.count(tenant=TENANT) == 10

    _empty_the_mount(vault)
    result = await indexer.run()

    assert result["fuse_tripped"] == 1
    assert result["deleted"] == 0
    # The ledger and the vectors are exactly as they were — nothing was reconciled away.
    assert await note_index.count(tenant=TENANT) == 10
    indexer._qdrant.delete.assert_not_awaited()  # type: ignore[attr-defined]
    assert indexer.fuse.tripped is True


async def test_run_refuses_a_deletion_above_the_threshold(
    note_index: NoteIndex, vault: Path
) -> None:
    indexer = _indexer(note_index, vault)
    await indexer.run()

    for n in range(6):  # 6 of 10 — over half, over the floor
        (vault / f"note_{n}.md").unlink()
    result = await indexer.run()

    assert result["fuse_tripped"] == 1
    assert await note_index.count(tenant=TENANT) == 10


async def test_run_still_prunes_an_ordinary_deletion(note_index: NoteIndex, vault: Path) -> None:
    """Below the ratio the fuse is invisible: the index still converges on the vault."""
    indexer = _indexer(note_index, vault)
    await indexer.run()

    (vault / "note_0.md").unlink()
    (vault / "note_1.md").unlink()
    result = await indexer.run()

    assert result["fuse_tripped"] == 0
    assert result["deleted"] == 2
    assert await note_index.count(tenant=TENANT) == 8
    assert indexer.fuse.tripped is False


async def test_first_index_of_an_empty_vault_does_not_trip(
    note_index: NoteIndex, tmp_path: Path
) -> None:
    """A fresh install indexes an empty vault every time; that must stay unremarkable."""
    indexer = _indexer(note_index, tmp_path)
    result = await indexer.run()
    assert result == {"indexed": 0, "deleted": 0, "unchanged": 0, "fuse_tripped": 0}
    assert indexer.fuse.tripped is False


async def test_force_runs_the_deletion_the_fuse_refused(note_index: NoteIndex, vault: Path) -> None:
    indexer = _indexer(note_index, vault)
    await indexer.run()
    _empty_the_mount(vault)
    assert (await indexer.run())["fuse_tripped"] == 1

    result = await indexer.run(force=True)

    assert result["fuse_tripped"] == 0
    assert result["deleted"] == 10
    assert await note_index.count(tenant=TENANT) == 0


async def test_a_clean_pass_re_arms_the_fuse(note_index: NoteIndex, vault: Path) -> None:
    """The mount is repaired; the next pass must clear the tripped state, not keep nagging."""
    indexer = _indexer(note_index, vault)
    await indexer.run()
    contents = {note.name: note.read_text() for note in vault.glob("*.md")}
    _empty_the_mount(vault)
    await indexer.run()
    assert indexer.fuse.tripped is True

    for name, body in contents.items():  # the "mount comes back" moment
        (vault / name).write_text(body)
    result = await indexer.run()

    assert result["fuse_tripped"] == 0
    assert indexer.fuse.tripped is False
    assert await note_index.count(tenant=TENANT) == 10


async def test_disabled_fuse_restores_the_old_behaviour(note_index: NoteIndex, vault: Path) -> None:
    indexer = _indexer(note_index, vault, enabled=False)
    await indexer.run()
    _empty_the_mount(vault)

    result = await indexer.run()

    assert result["fuse_tripped"] == 0
    assert result["deleted"] == 10
    assert await note_index.count(tenant=TENANT) == 0


async def test_fuse_is_tenant_scoped(note_index: NoteIndex, vault: Path) -> None:
    """Two tenants over one ledger: one tenant's empty mount never touches the other's rows."""
    other = KnowledgeIndexer(note_index, _qdrant(), _platform(), vault_path=vault, tenant="other")
    mine = _indexer(note_index, vault)
    await mine.run()
    await other.run()
    assert await note_index.count(tenant="other") == 10

    _empty_the_mount(vault)
    assert (await mine.run())["fuse_tripped"] == 1

    assert await note_index.count(tenant=TENANT) == 10
    assert await note_index.count(tenant="other") == 10
    assert other.fuse.tripped is False  # a different tenant's fuse is a different fuse


# ── Reconcile-time GC (#470) is fused too ─────────────────────────────────────


async def test_reconcile_gc_refuses_a_wholesale_disappearance(
    note_index: NoteIndex, vault: Path
) -> None:
    """``_gc_stale`` stats each ledger row; a vanished vault must not GC every one of them."""
    indexer = _indexer(note_index, vault)
    await indexer.run()
    _empty_the_mount(vault)

    changed = await indexer.reconcile()

    assert changed is False
    assert await note_index.count(tenant=TENANT) == 10
    assert indexer.fuse.tripped is True


async def test_reconcile_gc_still_removes_a_single_orphan(
    note_index: NoteIndex, vault: Path
) -> None:
    indexer = _indexer(note_index, vault)
    await indexer.run()
    (vault / "note_0.md").unlink()

    assert await indexer.reconcile() is True
    assert await note_index.count(tenant=TENANT) == 9


async def test_reconcile_gc_can_be_forced(note_index: NoteIndex, vault: Path) -> None:
    indexer = _indexer(note_index, vault)
    await indexer.run()
    _empty_the_mount(vault)

    assert await indexer.reconcile(force=True) is True
    assert await note_index.count(tenant=TENANT) == 0


# ── The POST /reindex pre-check ───────────────────────────────────────────────


async def test_check_source_fuse_sees_the_risk_before_a_reset(
    note_index: NoteIndex, vault: Path
) -> None:
    """The whole point of the pre-check: ``reset`` would erase the evidence of the risk."""
    indexer = _indexer(note_index, vault)
    await indexer.run()
    _empty_the_mount(vault)

    assert await indexer.check_source_fuse() is not None
    assert await indexer.check_source_fuse(force=True) is None

    await indexer.reset()  # the ledger is gone — and with it anything to protect
    assert await indexer.check_source_fuse() is None


async def test_rebuild_refusals_records_every_refusing_source(
    note_index: NoteIndex, vault: Path
) -> None:
    healthy = _indexer(note_index, vault)
    suspect = _indexer(note_index, vault)
    await healthy.run()

    class _Suspect:
        """A source whose rebuild would be a wipe, wired like the real indexers."""

        def __init__(self) -> None:
            self.fuse = IndexFuse(tenant=TENANT, source="docs")

        async def check_source_fuse(self, *, force: bool = False) -> FuseTrip | None:
            return self.fuse.evaluate(
                ledger_rows=12, would_delete=12, source_entries=0, force=force
            )

    bad = _Suspect()
    trips = await rebuild_refusals([healthy, bad])

    assert [t.source for t in trips] == ["docs"]
    assert bad.fuse.tripped is True  # recorded, so /status and /metrics see it
    assert healthy.fuse.tripped is False
    assert suspect.fuse.tripped is False
