"""Tests for the vault file-watcher (#232).

The loop is driven deterministically by an **injected** change stream, so most tests
need no real filesystem timing. One end-to-end test exercises the real
``watchfiles.awatch`` path (with polling, for cross-platform reliability) to prove a
genuine on-disk write reaches the indexer.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path

import pytest
from watchfiles import Change

from epicurus_knowledge.watcher import FileChange, VaultChangeFilter, VaultWatcher


class _RecordingIndexer:
    """Counts ``run`` calls; optionally raises the first *fail_times* times."""

    def __init__(self, *, fail_times: int = 0) -> None:
        self.runs = 0
        self._fail_times = fail_times

    async def run(self) -> dict[str, int]:
        self.runs += 1
        if self.runs <= self._fail_times:
            raise RuntimeError("core paused")
        return {"indexed": 1, "deleted": 0, "unchanged": 2}


def _stream(*batches: set[FileChange]):
    """A change-stream factory yielding the given batches once, then stopping."""

    async def _gen() -> AsyncIterator[set[FileChange]]:
        for batch in batches:
            yield batch

    return _gen


# ── VaultChangeFilter ─────────────────────────────────────────────────────────


def test_filter_keeps_markdown_notes() -> None:
    f = VaultChangeFilter()
    assert f(Change.modified, "/vault/note.md")
    assert f(Change.added, "/vault/sub/deep/note.md")
    # A deletion still carries the .md path, so removals pass through to the next pass.
    assert f(Change.deleted, "/vault/gone.md")


def test_filter_ignores_obsidian_trash_and_non_markdown() -> None:
    f = VaultChangeFilter()
    assert not f(Change.modified, "/vault/.obsidian/workspace.json")
    assert not f(Change.deleted, "/vault/.trash/old.md")
    assert not f(Change.modified, "/vault/image.png")
    assert not f(Change.added, "/vault/notes.txt")


# ── VaultWatcher loop (injected stream) ───────────────────────────────────────


async def test_reindexes_once_per_batch(tmp_path: Path) -> None:
    idx = _RecordingIndexer()
    watcher = VaultWatcher(
        tmp_path,
        idx,
        watch=_stream(
            {(Change.modified, str(tmp_path / "a.md"))},
            {(Change.added, str(tmp_path / "b.md")), (Change.modified, str(tmp_path / "c.md"))},
        ),
    )
    await watcher.run()
    # Two coalesced batches → two incremental passes (the indexer skips unchanged files).
    assert idx.runs == 2


async def test_empty_batch_does_not_reindex(tmp_path: Path) -> None:
    idx = _RecordingIndexer()
    watcher = VaultWatcher(tmp_path, idx, watch=_stream(set()))
    await watcher.run()
    assert idx.runs == 0


async def test_reindex_failure_is_swallowed_and_retried(tmp_path: Path) -> None:
    # The first pass fails (e.g. the core is paused mid-embed); the watcher must not die,
    # and the next change must still trigger a fresh pass.
    idx = _RecordingIndexer(fail_times=1)
    watcher = VaultWatcher(
        tmp_path,
        idx,
        watch=_stream(
            {(Change.modified, str(tmp_path / "a.md"))},
            {(Change.modified, str(tmp_path / "a.md"))},
        ),
    )
    await watcher.run()  # must not raise
    assert idx.runs == 2


async def test_missing_vault_is_idle() -> None:
    idx = _RecordingIndexer()
    # Inject a stream that would reindex if reached — the existence guard must short-circuit.
    watcher = VaultWatcher(
        Path("/no/such/vault"),
        idx,
        watch=_stream({(Change.modified, "/no/such/vault/a.md")}),
    )
    await watcher.run()
    assert idx.runs == 0


def test_stop_signals_the_loop(tmp_path: Path) -> None:
    watcher = VaultWatcher(tmp_path, _RecordingIndexer())
    assert not watcher._stop.is_set()
    watcher.stop()
    assert watcher._stop.is_set()


# ── Real watchfiles integration ───────────────────────────────────────────────


async def test_real_write_triggers_reindex(tmp_path: Path) -> None:
    """A genuine on-disk write under the vault drives a real ``awatch`` pass.

    Polling (``force_polling``) keeps this deterministic across platforms/CI where native
    FS events are unreliable; the short debounce keeps the test quick.
    """
    from watchfiles import awatch

    (tmp_path / "seed.md").write_text("# seed", encoding="utf-8")
    fired = asyncio.Event()

    class _SignallingIndexer:
        def __init__(self) -> None:
            self.runs = 0

        async def run(self) -> dict[str, int]:
            self.runs += 1
            fired.set()
            return {"indexed": 1, "deleted": 0, "unchanged": 0}

    idx = _SignallingIndexer()
    watcher = VaultWatcher(
        tmp_path,
        idx,
        watch=lambda: awatch(
            tmp_path,
            watch_filter=VaultChangeFilter(),
            debounce=50,
            step=5,
            force_polling=True,
        ),
    )
    task = asyncio.create_task(watcher.run())
    try:
        await asyncio.sleep(0.3)  # let the watcher take its baseline before we write
        (tmp_path / "new.md").write_text("# a new note", encoding="utf-8")
        await asyncio.wait_for(fired.wait(), timeout=15)
    finally:
        watcher.stop()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    assert idx.runs >= 1


# Belt-and-braces: the awatch-backed default stream is a real async iterator.
async def test_default_watch_factory_builds(tmp_path: Path) -> None:
    watcher = VaultWatcher(tmp_path, _RecordingIndexer(), debounce_ms=200)
    source = watcher._default_watch()
    assert hasattr(source, "__aiter__")
    # Close the generator we opened so it doesn't linger as an unstarted watcher.
    aclose = getattr(source, "aclose", None)
    if aclose is not None:
        with suppress(Exception):
            await aclose()


@pytest.mark.parametrize("debounce", [0, -5])
def test_debounce_is_floored_to_one(tmp_path: Path, debounce: int) -> None:
    watcher = VaultWatcher(tmp_path, _RecordingIndexer(), debounce_ms=debounce)
    assert watcher._debounce_ms == 1
