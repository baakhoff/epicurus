"""Tests for the files-tree watcher (#390).

The loop is driven deterministically by an **injected** change stream, so most tests need
no real filesystem timing. The rescan callback the watcher drives is the real
:func:`~epicurus_storage.scanner.scan` over an in-memory SQLite :class:`FileIndex` and a tmp
tree, so a driven batch proves the index actually reflects an on-disk change. One end-to-end
test exercises the real ``watchfiles.awatch`` path (with polling, for cross-platform
reliability) to prove a genuine write reaches the rescan. Every test that drives ``run`` is
wrapped in ``asyncio.wait_for`` so a logic bug fails loudly instead of hanging the suite.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from watchfiles import Change

from epicurus_storage.db import FileIndex
from epicurus_storage.scanner import scan
from epicurus_storage.watcher import FileChange, FilesWatcher, WatchSource

TENANT = "test"

# A generous ceiling: every driven loop yields a finite, injected stream, so it must end
# well within this. If it doesn't, the watcher is hung — fail loudly rather than wait it out.
_RUN_TIMEOUT = 10.0


@pytest.fixture
async def tmp_index() -> FileIndex:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    idx = FileIndex(engine)
    await idx.init()
    return idx


@pytest.fixture
def sample_tree(tmp_path: Path) -> Path:
    """A small tree: docs/readme.txt + docs/notes.md."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "readme.txt").write_text("0123456789")
    (tmp_path / "docs" / "notes.md").write_text("hello")
    return tmp_path


def _stream(*batches: set[FileChange]) -> WatchSource:
    """A change-stream factory yielding the given batches once, then stopping."""

    async def _gen() -> AsyncIterator[set[FileChange]]:
        for batch in batches:
            yield batch

    return _gen


class _RecordingRescan:
    """Counts rescan calls; optionally raises the first *fail_times* times."""

    def __init__(self, *, fail_times: int = 0) -> None:
        self.runs = 0
        self._fail_times = fail_times

    async def __call__(self) -> int:
        self.runs += 1
        if self.runs <= self._fail_times:
            raise RuntimeError("db blip")
        return 0


# ── Index is actually updated by a driven batch ───────────────────────────────


async def test_change_batch_triggers_rescan_that_updates_index(
    tmp_index: FileIndex, sample_tree: Path
) -> None:
    # Index starts empty; a file then appears on disk and a change batch drives a rescan.
    async def _rescan() -> int:
        return await scan(sample_tree, tmp_index, tenant=TENANT)

    assert (await tmp_index.search(tenant=TENANT, query="late")) == []
    (sample_tree / "docs" / "late.md").write_text("arrived after startup")

    watcher = FilesWatcher(
        sample_tree,
        _rescan,
        watch=_stream({(Change.added, str(sample_tree / "docs" / "late.md"))}),
    )
    await asyncio.wait_for(watcher.run(), timeout=_RUN_TIMEOUT)

    results = await tmp_index.search(tenant=TENANT, query="late")
    assert {r.name for r in results} == {"late.md"}


async def test_delete_is_picked_up_by_rescan(tmp_index: FileIndex, sample_tree: Path) -> None:
    # A full scan is an idempotent sync, so a rescan after a delete purges the stale row.
    async def _rescan() -> int:
        return await scan(sample_tree, tmp_index, tenant=TENANT)

    await scan(sample_tree, tmp_index, tenant=TENANT)
    assert await tmp_index.search(tenant=TENANT, query="readme")
    (sample_tree / "docs" / "readme.txt").unlink()

    watcher = FilesWatcher(
        sample_tree,
        _rescan,
        watch=_stream({(Change.deleted, str(sample_tree / "docs" / "readme.txt"))}),
    )
    await asyncio.wait_for(watcher.run(), timeout=_RUN_TIMEOUT)

    assert await tmp_index.search(tenant=TENANT, query="readme") == []


# ── Loop mechanics (injected stream) ──────────────────────────────────────────


async def test_rescans_once_per_batch(tmp_path: Path) -> None:
    rescan = _RecordingRescan()
    watcher = FilesWatcher(
        tmp_path,
        rescan,
        watch=_stream(
            {(Change.modified, str(tmp_path / "a.txt"))},
            {(Change.added, str(tmp_path / "b.txt")), (Change.modified, str(tmp_path / "c.txt"))},
        ),
    )
    await asyncio.wait_for(watcher.run(), timeout=_RUN_TIMEOUT)
    assert rescan.runs == 2  # two coalesced batches → two incremental passes


async def test_empty_batch_does_not_rescan(tmp_path: Path) -> None:
    rescan = _RecordingRescan()
    watcher = FilesWatcher(tmp_path, rescan, watch=_stream(set()))
    await asyncio.wait_for(watcher.run(), timeout=_RUN_TIMEOUT)
    assert rescan.runs == 0


async def test_rescan_failure_is_swallowed_and_retried(tmp_path: Path) -> None:
    # The first pass fails (e.g. a DB blip); the watcher must not die, and the next change
    # must still trigger a fresh pass.
    rescan = _RecordingRescan(fail_times=1)
    watcher = FilesWatcher(
        tmp_path,
        rescan,
        watch=_stream(
            {(Change.modified, str(tmp_path / "a.txt"))},
            {(Change.modified, str(tmp_path / "a.txt"))},
        ),
    )
    await asyncio.wait_for(watcher.run(), timeout=_RUN_TIMEOUT)  # must not raise
    assert rescan.runs == 2


async def test_missing_root_is_idle() -> None:
    rescan = _RecordingRescan()
    # Inject a stream that would rescan if reached — the existence guard must short-circuit.
    watcher = FilesWatcher(
        Path("/no/such/root"),
        rescan,
        watch=_stream({(Change.modified, "/no/such/root/a.txt")}),
    )
    await asyncio.wait_for(watcher.run(), timeout=_RUN_TIMEOUT)
    assert rescan.runs == 0


def test_stop_signals_the_loop(tmp_path: Path) -> None:
    watcher = FilesWatcher(tmp_path, _RecordingRescan())
    assert not watcher._stop.is_set()
    watcher.stop()
    assert watcher._stop.is_set()


async def test_stop_ends_the_loop(tmp_path: Path) -> None:
    """A stop set before the stream is exhausted ends the loop cleanly (no hang/raise)."""
    started = asyncio.Event()

    async def _blocking_stream() -> AsyncIterator[set[FileChange]]:
        # Yield one batch, then block until stop is signalled — modelling awatch's stop_event.
        yield {(Change.modified, str(tmp_path / "a.txt"))}
        started.set()
        await watcher._stop.wait()

    rescan = _RecordingRescan()
    watcher = FilesWatcher(tmp_path, rescan, watch=_blocking_stream)
    task = asyncio.create_task(watcher.run())
    await asyncio.wait_for(started.wait(), timeout=_RUN_TIMEOUT)
    watcher.stop()
    await asyncio.wait_for(task, timeout=_RUN_TIMEOUT)  # must return, not hang
    assert rescan.runs == 1


@pytest.mark.parametrize("debounce", [0, -5])
def test_debounce_is_floored_to_one(tmp_path: Path, debounce: int) -> None:
    watcher = FilesWatcher(tmp_path, _RecordingRescan(), debounce_ms=debounce)
    assert watcher._debounce_ms == 1


# ── The scan_lock serialises concurrent rescans ───────────────────────────────


async def test_scan_lock_serialises_concurrent_rescans(
    tmp_index: FileIndex, sample_tree: Path
) -> None:
    """The lifespan wraps scan behind a lock; concurrent rescans must not overlap.

    Mirrors the lifespan's ``_rescan`` (``async with scan_lock``) and asserts that two
    rescans driven at once never run their critical sections concurrently.
    """
    scan_lock = asyncio.Lock()
    overlap = False
    active = 0

    async def _rescan() -> int:
        nonlocal overlap, active
        async with scan_lock:
            active += 1
            if active > 1:
                overlap = True
            await asyncio.sleep(0.02)  # widen the window a real walk would occupy
            total = await scan(sample_tree, tmp_index, tenant=TENANT)
            active -= 1
            return total

    await asyncio.gather(_rescan(), _rescan())
    assert overlap is False


# ── Real watchfiles integration ───────────────────────────────────────────────


async def test_real_write_triggers_rescan(tmp_index: FileIndex, sample_tree: Path) -> None:
    """A genuine on-disk write under the tree drives a real ``awatch`` pass.

    Polling (``force_polling``) keeps this deterministic across platforms/CI where native FS
    events are unreliable; the short debounce keeps the test quick.
    """
    from watchfiles import DefaultFilter, awatch

    fired = asyncio.Event()

    async def _rescan() -> int:
        total = await scan(sample_tree, tmp_index, tenant=TENANT)
        fired.set()
        return total

    watcher = FilesWatcher(
        sample_tree,
        _rescan,
        watch=lambda: awatch(
            sample_tree,
            watch_filter=DefaultFilter(),
            debounce=50,
            step=5,
            force_polling=True,
        ),
    )
    task = asyncio.create_task(watcher.run())
    try:
        await asyncio.sleep(0.3)  # let the watcher take its baseline before we write
        (sample_tree / "docs" / "fresh.txt").write_text("a new file")
        await asyncio.wait_for(fired.wait(), timeout=15)
    finally:
        watcher.stop()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    results = await tmp_index.search(tenant=TENANT, query="fresh")
    assert {r.name for r in results} == {"fresh.txt"}


# Belt-and-braces: the awatch-backed default stream is a real async iterator.
async def test_default_watch_factory_builds(tmp_path: Path) -> None:
    watcher = FilesWatcher(tmp_path, _RecordingRescan(), debounce_ms=200)
    source = watcher._default_watch()
    assert hasattr(source, "__aiter__")
    # Close the generator we opened so it doesn't linger as an unstarted watcher.
    aclose = getattr(source, "aclose", None)
    if aclose is not None:
        with suppress(Exception):
            await aclose()
