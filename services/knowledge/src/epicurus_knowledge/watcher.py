"""Vault file-watcher → incremental re-index (#232, ADR-0035).

When the operator bind-mounts an externally-synced vault — an Obsidian Sync folder,
a Git working copy, or any directory another process keeps in step — and enables
``VAULT_WATCH``, this watches the vault and triggers an **incremental** re-index after a
debounced quiet window. Edits that land on disk (via Obsidian, a Git pull, or a plain
editor) are then reflected in search within a bounded delay, with no manual
``knowledge_reindex``.

Why this is cheap: :class:`~epicurus_knowledge.indexer.KnowledgeIndexer` is hash/mtime
incremental, so a watch event over a synced folder only re-embeds the files that actually
changed — everything else is skipped on a content-hash compare. Obsidian Sync lands many
files in a burst, so the watcher **coalesces** changes over a debounce window into a single
pass (the underlying ``watchfiles.awatch`` groups events; we re-index once per yielded
batch) and the indexer's own re-entrancy lock serialises that pass against the startup
index. Obsidian's ``.obsidian/`` config dir and ``.trash/`` are ignored, and only ``.md``
files trigger a pass, so config churn and non-note files never cause needless work.

The watcher only **reads** the vault. In watch mode the vault is treated as externally
owned (ADR-0035): epicurus never writes it, so there is no two-writer conflict for the
watcher to reconcile — deletions and edits made elsewhere simply flow into the index on the
next pass (the incremental run purges vectors for files gone from disk).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Protocol

from watchfiles import Change, DefaultFilter, awatch

from epicurus_core import get_logger

_log = get_logger("knowledge.watcher")

# One filesystem change as yielded by watchfiles: (kind, absolute-path).
FileChange = tuple[Change, str]
# A factory for the change stream — injected in tests to drive the loop deterministically.
WatchSource = Callable[[], AsyncIterator[set[FileChange]]]


class _Reindexer(Protocol):
    """The slice of :class:`KnowledgeIndexer` the watcher drives."""

    async def run(self) -> dict[str, int]: ...


class VaultChangeFilter(DefaultFilter):
    """Restrict watch events to vault notes worth re-indexing.

    Extends watchfiles' :class:`DefaultFilter` (which already drops ``.git``,
    ``__pycache__``, editor swap files, etc.) by also ignoring Obsidian's own
    ``.obsidian/`` config directory and its ``.trash/`` folder, then keeping only
    ``.md`` paths — the sole files the indexer reads. A deletion still carries the
    ``.md`` path, so removals pass through and the next incremental pass purges them.
    """

    # Directories whose contents never warrant a re-index, on top of DefaultFilter's set.
    ignore_dirs = (*DefaultFilter.ignore_dirs, ".obsidian", ".trash")

    def __call__(self, change: Change, path: str) -> bool:
        # watchfiles emits OS-native paths and DefaultFilter splits the ignore-dir check on
        # os.sep; normalise first so a forward-slash path is filtered correctly on Windows
        # too (and the filter is portable in tests). The .md gate matches the only files the
        # indexer reads — a deletion still carries its .md path, so removals pass through.
        native = path.replace("/", os.sep)
        return super().__call__(change, native) and native.endswith(".md")


class VaultWatcher:
    """Watch the vault directory and incrementally re-index on change.

    The watcher is started as a background task by the app's lifespan when
    ``VAULT_WATCH`` is set; it runs until cancelled or :meth:`stop` is called.

    Args:
        vault_path: Vault root to watch (recursively).
        indexer: The vault indexer; its ``run`` is incremental, so each pass only
            re-embeds files whose content hash changed.
        debounce_ms: Coalescing window for a burst of changes before a pass fires.
        watch: Optional change-stream factory (defaults to ``watchfiles.awatch`` over
            the vault). Injected in tests so the loop can be driven deterministically.
        on_synced: Optional async callback invoked with the pass's counts
            (``{indexed, deleted, unchanged}``) after each successful re-index — the
            spine's one-batch-event-per-pass hook (#665). Failures are swallowed.
        on_failed: Optional async callback invoked with the error string when a pass
            fails — the spine's rate-limited ``index_failed`` hook (#665). Failures
            are swallowed.
    """

    def __init__(
        self,
        vault_path: Path,
        indexer: _Reindexer,
        *,
        debounce_ms: int = 1500,
        watch: WatchSource | None = None,
        on_synced: Callable[[dict[str, int]], Awaitable[None]] | None = None,
        on_failed: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._vault = vault_path
        self._indexer = indexer
        self._debounce_ms = max(1, debounce_ms)
        self._stop = asyncio.Event()
        self._watch = watch
        self._on_synced = on_synced
        self._on_failed = on_failed

    def _default_watch(self) -> AsyncIterator[set[FileChange]]:
        """The real change stream: ``awatch`` over the vault, filtered and debounced."""
        return awatch(
            self._vault,
            watch_filter=VaultChangeFilter(),
            debounce=self._debounce_ms,
            stop_event=self._stop,
        )

    async def run(self) -> None:
        """Re-index the vault on every debounced batch of relevant changes until stopped.

        Missing vault → the watcher stays idle (logs once and returns) rather than crash
        the service: ``VAULT_WATCH`` may be set before the bind-mount target exists. A
        failed pass (e.g. the core is paused mid-embed) is logged and swallowed so the
        next change still triggers a retry — the watcher never dies on a transient error.
        """
        if not self._vault.exists():
            _log.warning("vault path does not exist; watcher idle", path=str(self._vault))
            return

        source = self._watch() if self._watch is not None else self._default_watch()
        _log.info(
            "vault watcher started",
            path=str(self._vault),
            debounce_ms=self._debounce_ms,
        )
        async for batch in source:
            changed = sorted({path for _, path in batch})
            if not changed:  # defensive — the filter usually keeps only relevant paths
                continue
            await self._reindex(changed)
        _log.info("vault watcher stopped", path=str(self._vault))

    async def _reindex(self, changed: list[str]) -> None:
        """Run one incremental pass for a coalesced batch of changed paths."""
        _log.info("vault changed on disk; re-indexing", changed=len(changed))
        try:
            result = await self._indexer.run()
        except Exception as exc:  # transient (core paused, qdrant blip) — retry next change
            _log.warning(
                "watch-triggered re-index failed; will retry on next change",
                error=str(exc),
            )
            if self._on_failed is not None:
                try:
                    await self._on_failed(str(exc))
                except Exception as cb_exc:  # observability only — never break the loop
                    _log.warning("watcher on_failed callback raised", error=str(cb_exc))
            return
        _log.info("watch re-index complete", **result)
        # One batch announcement per pass (#665) — the callback (the spine emitter) skips
        # no-op passes itself; a callback failure never breaks the watch loop.
        if self._on_synced is not None:
            try:
                await self._on_synced(dict(result))
            except Exception as cb_exc:
                _log.warning("watcher on_synced callback raised", error=str(cb_exc))

    def stop(self) -> None:
        """Signal the watch loop to stop (the lifespan also cancels the task as a backstop)."""
        self._stop.set()
