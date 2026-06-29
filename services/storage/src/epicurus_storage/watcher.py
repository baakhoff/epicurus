"""Files-tree watcher → incremental rescan on change (#390, ADR-0057).

The storage index walks the served tree **once at startup** (``app.py`` lifespan). Anything
that lands in the shared file space afterwards — a module dropping a file, an external write,
a sync — went unindexed until a restart or a manual ``storage_rescan``, leaving the Files UI
and storage/knowledge name search showing a stale tree. This watches the served root and
triggers an **incremental** rescan after a debounced quiet window, so creates / modifies /
deletes on disk are reflected within a bounded delay with no manual rescan.

Why a full :func:`~epicurus_storage.scanner.scan` per change is the right "incremental"
pass: ``scan`` upserts every entry it sees and then purges (``purge_stale``) the unseen
``source="fs"`` rows, so one walk is an idempotent sync — new/changed files are upserted and
vanished files
are pruned, while uploaded/agent-written objects (``source="object"``) survive untouched. The
walk is pure DB I/O (no embeddings), so it is cheap; a burst of events is **coalesced** over a
debounce window into a single pass (``watchfiles.awatch`` groups events and we rescan once per
yielded batch), and a caller-supplied lock serialises that pass against the startup scan so the
two never walk the tree at once (``scan`` itself holds no lock).

This mirrors the knowledge vault-watcher (ADR-0035), with two deliberate differences: storage
indexes **all** files (not just ``.md``), so it filters with watchfiles' :class:`DefaultFilter`
directly — which already drops ``.git``, ``__pycache__``, and editor swap files, avoiding
needless rescan storms — and the watcher drives a plain ``rescan`` callable rather than an
indexer object, keeping it decoupled from how the rescan is wired in the lifespan. The watcher
only **reads** the tree; the served root is mounted read-only.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from watchfiles import Change, DefaultFilter, awatch

from epicurus_core import get_logger

_log = get_logger("storage.watcher")

# One filesystem change as yielded by watchfiles: (kind, absolute-path).
FileChange = tuple[Change, str]
# A factory for the change stream — injected in tests to drive the loop deterministically.
WatchSource = Callable[[], AsyncIterator[set[FileChange]]]


class FilesWatcher:
    """Watch the served files tree and trigger an incremental rescan on change.

    Started as a background task by the app's lifespan when ``STORAGE_WATCH`` is set; it
    runs until cancelled or :meth:`stop` is called.

    Args:
        root: The served tenant subtree to watch (recursively).
        rescan: A coroutine factory that performs one incremental rescan (the lifespan
            wraps :func:`~epicurus_storage.scanner.scan` behind a lock and passes it here).
            Its return value is logged, then discarded.
        debounce_ms: Coalescing window for a burst of changes before a rescan fires.
        watch: Optional change-stream factory (defaults to ``watchfiles.awatch`` over
            *root*, filtered by :class:`DefaultFilter` and debounced). Injected in tests so
            the loop can be driven deterministically without real filesystem timing.
    """

    def __init__(
        self,
        root: Path,
        rescan: Callable[[], Awaitable[Any]],
        *,
        debounce_ms: int = 1500,
        watch: WatchSource | None = None,
    ) -> None:
        self._root = root
        self._rescan = rescan
        self._debounce_ms = max(1, debounce_ms)
        self._stop = asyncio.Event()
        self._watch = watch

    def _default_watch(self) -> AsyncIterator[set[FileChange]]:
        """The real change stream: ``awatch`` over the tree, filtered and debounced.

        :class:`DefaultFilter` already drops ``.git``, ``__pycache__``, and editor swap
        files; storage indexes everything else, so no extension gate is applied.
        """
        return awatch(
            self._root,
            watch_filter=DefaultFilter(),
            debounce=self._debounce_ms,
            stop_event=self._stop,
        )

    async def run(self) -> None:
        """Rescan the tree on every debounced batch of changes until stopped.

        Missing root → the watcher stays idle (logs once and returns) rather than crash the
        service: ``STORAGE_WATCH`` may be set before the bind-mount target exists. A failed
        rescan (e.g. the DB blips) is logged and swallowed so the next change still triggers
        a retry — the watcher never dies on a transient error.
        """
        if not self._root.exists():
            _log.warning("storage root does not exist; watcher idle", path=str(self._root))
            return

        source = self._watch() if self._watch is not None else self._default_watch()
        _log.info(
            "files watcher started",
            path=str(self._root),
            debounce_ms=self._debounce_ms,
        )
        async for batch in source:
            changed = sorted({path for _, path in batch})
            if not changed:  # defensive — the filter usually keeps only relevant paths
                continue
            await self._do_rescan(changed)
        _log.info("files watcher stopped", path=str(self._root))

    async def _do_rescan(self, changed: list[str]) -> None:
        """Run one incremental rescan for a coalesced batch of changed paths."""
        _log.info("files changed on disk; rescanning", changed=len(changed))
        try:
            result = await self._rescan()
        except Exception as exc:  # transient (DB blip, fs race) — retry on the next change
            _log.warning(
                "watch-triggered rescan failed; will retry on next change",
                error=str(exc),
            )
            return
        _log.info("watch rescan complete", visited=result)

    def stop(self) -> None:
        """Signal the watch loop to stop (the lifespan also cancels the task as a backstop)."""
        self._stop.set()
