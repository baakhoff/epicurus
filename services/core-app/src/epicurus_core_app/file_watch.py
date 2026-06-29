"""File-space watcher → incremental rescan on change (ADR-0063, after ADR-0057).

The core walks the file space once at startup (:func:`file_scan.scan`). Anything that lands
afterwards — a module writing through the file API, an external write into a bind-mounted
tree, an Obsidian-Sync drop — went unindexed until the next manual rescan, leaving the Files
UI and file search stale. This watches the local tenant tree and triggers an **incremental**
rescan after a debounced quiet window, so creates / modifies / deletes are reflected within a
bounded delay.

It mirrors the storage watcher it replaces (ADR-0057): one :func:`~file_scan.scan` per
debounced batch is an idempotent sync (upsert every seen entry, purge the unseen), the walk is
pure DB I/O (no embeddings) so it is cheap, ``watchfiles.awatch`` coalesces a burst of events
into one pass, and a caller-supplied lock serialises that pass against the startup scan. Only
the **local** backend has a real filesystem to watch; the S3 backend has no inotify surface, so
the lifespan starts the watcher only for ``FILES_BACKEND=local``. The watcher only **reads** the
tree.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from watchfiles import Change, DefaultFilter, awatch

from epicurus_core import get_logger

_log = get_logger("core.file_watch")

# One filesystem change as yielded by watchfiles: (kind, absolute-path).
FileChange = tuple[Change, str]
# A factory for the change stream — injected in tests to drive the loop deterministically.
WatchSource = Callable[[], AsyncIterator[set[FileChange]]]


class FileWatcher:
    """Watch the local file-space tree and trigger an incremental rescan on change.

    Started as a background task by the app's lifespan when the backend is local; it runs
    until cancelled or :meth:`stop` is called.

    Args:
        root: The local tenant subtree to watch (recursively).
        rescan: A coroutine factory that performs one incremental rescan (the lifespan wraps
            :func:`~file_scan.scan` behind a lock and passes it here). Its return value is
            logged, then discarded.
        debounce_ms: Coalescing window for a burst of changes before a rescan fires.
        watch: Optional change-stream factory (defaults to ``watchfiles.awatch`` over *root*,
            filtered by :class:`DefaultFilter` and debounced). Injected in tests so the loop
            can be driven deterministically without real filesystem timing.
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
        files; the core indexes everything else, so no extension gate is applied.
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
        core: the tenant tree may not exist yet on first boot. A failed rescan (e.g. the DB
        blips) is logged and swallowed so the next change still triggers a retry — the watcher
        never dies on a transient error.
        """
        if not self._root.exists():
            _log.warning("file-space root does not exist; watcher idle", path=str(self._root))
            return

        source = self._watch() if self._watch is not None else self._default_watch()
        _log.info("file watcher started", path=str(self._root), debounce_ms=self._debounce_ms)
        async for batch in source:
            changed = sorted({path for _, path in batch})
            if not changed:  # defensive — the filter usually keeps only relevant paths
                continue
            await self._do_rescan(changed)
        _log.info("file watcher stopped", path=str(self._root))

    async def _do_rescan(self, changed: list[str]) -> None:
        """Run one incremental rescan for a coalesced batch of changed paths."""
        _log.info("file space changed on disk; rescanning", changed=len(changed))
        try:
            result = await self._rescan()
        except Exception as exc:  # transient (DB blip, fs race) — retry on the next change
            _log.warning("watch-triggered rescan failed; will retry on next change", error=str(exc))
            return
        _log.info("watch rescan complete", visited=result)

    def stop(self) -> None:
        """Signal the watch loop to stop (the lifespan also cancels the task as a backstop)."""
        self._stop.set()
