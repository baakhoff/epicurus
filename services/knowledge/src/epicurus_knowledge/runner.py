"""Resilient initial-index runner (#230).

The knowledge service must serve ``/health`` immediately, yet a cold ``compose up``
may start it before core-app / qdrant are ready, and a first index over a real vault
plus the bundled docs takes minutes. :class:`IndexRunner` decouples the two: the app
launches it as a background task and yields at once, while the runner indexes every
source with retry/backoff and exposes progress for ``GET /status``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from epicurus_core import get_logger

_log = get_logger("knowledge.runner")

# Index outcome counts, summed across sources.
Counts = dict[str, int]
_COUNT_KEYS = ("indexed", "deleted", "unchanged")


class SourceIndexer(Protocol):
    """Anything the runner can drive — vault, platform-docs, and module-docs indexers."""

    async def reconcile(self) -> bool:
        """Self-heal stale state before indexing (#229); ``True`` if it changed anything."""
        ...

    async def run(self) -> dict[str, int]: ...


@dataclass(slots=True)
class IndexState:
    """Live progress of the background index, surfaced on ``GET /status`` (#230).

    ``phase`` moves ``pending`` → ``indexing`` → ``ready`` on success, or cycles through
    ``retrying`` and lands on ``error`` if every attempt fails.
    """

    phase: str = "pending"
    attempts: int = 0
    error: str | None = None
    last_result: Counts | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "attempts": self.attempts,
            "error": self.error,
            "last_result": self.last_result,
        }


class IndexRunner:
    """Runs every source indexer once, retrying with capped exponential backoff (#230).

    Args:
        indexers: The source indexers to run, in order, each exposing ``async run()``.
        max_attempts: How many times to retry the whole pass before giving up.
        base_delay_seconds: First backoff delay; doubles each attempt.
        max_delay_seconds: Upper bound on the backoff delay.
        on_complete: Optional async callback invoked with the summed counts after a
            successful pass (e.g. to emit a NATS event); its failures are swallowed.
        on_failed: Optional async callback invoked with the last error once every
            attempt is exhausted (the spine's ``knowledge.index_failed`` hook, #665);
            its failures are swallowed.
    """

    def __init__(
        self,
        indexers: Sequence[SourceIndexer],
        *,
        max_attempts: int = 30,
        base_delay_seconds: float = 1.0,
        max_delay_seconds: float = 30.0,
        on_complete: Callable[[Counts], Awaitable[None]] | None = None,
        on_failed: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._indexers = list(indexers)
        self._max_attempts = max(1, max_attempts)
        self._base = max(0.0, base_delay_seconds)
        self._cap = max(0.0, max_delay_seconds)
        self._on_complete = on_complete
        # Invoked with the last error when the whole retry budget is exhausted — the
        # spine's rate-limited `knowledge.index_failed` hook (#665). Best-effort.
        self._on_failed = on_failed
        self.state = IndexState()

    async def run_once(self) -> Counts:
        """Run every indexer once and return the summed ``{indexed, deleted, unchanged}``.

        A reconcile pre-pass runs across *all* sources first (#229, #470), so any that share a
        Qdrant collection (the vault/platform-docs/module-docs all touch ``<tenant>__docs``)
        clear their stale ledgers before the first ``run`` recreates the collection, and any
        with a ledger row for a path that no longer exists gets it GC'd before the walk.
        """
        reconciled = 0
        for indexer in self._indexers:
            if await indexer.reconcile():
                reconciled += 1
        if reconciled:
            _log.warning(
                "reconcile changed source state (qdrant-reset recovery or stale-path GC)",
                sources=reconciled,
            )

        total: Counts = dict.fromkeys(_COUNT_KEYS, 0)
        for indexer in self._indexers:
            result = await indexer.run()
            for key in _COUNT_KEYS:
                total[key] += int(result.get(key, 0))
        return total

    def _backoff(self, attempt: int) -> float:
        """Capped exponential backoff for ``attempt`` (1-based)."""
        delay: float = self._base * (2.0 ** (attempt - 1))
        return min(self._cap, delay)

    async def run_with_retry(self) -> None:
        """Index until a full pass succeeds or attempts are exhausted.

        Re-running is safe: each indexer is incremental, so a retry only touches files
        that are still new or changed. Cancellation (app shutdown) propagates cleanly.
        """
        for attempt in range(1, self._max_attempts + 1):
            self.state.attempts = attempt
            self.state.phase = "indexing"
            try:
                total = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # deps not ready, embed failure, etc. — retry.
                self.state.error = str(exc)
                self.state.phase = "retrying"
                delay = self._backoff(attempt)
                _log.warning(
                    "initial index attempt failed; will retry",
                    attempt=attempt,
                    max_attempts=self._max_attempts,
                    error=str(exc),
                    retry_in_seconds=delay,
                )
                await asyncio.sleep(delay)
                continue

            self.state.phase = "ready"
            self.state.error = None
            self.state.last_result = total
            _log.info("initial index complete", attempt=attempt, **total)
            if self._on_complete is not None:
                try:
                    await self._on_complete(total)
                except Exception as exc:  # callback is best-effort, never fail the run.
                    _log.warning("index on_complete callback failed", error=str(exc))
            return

        self.state.phase = "error"
        _log.error(
            "initial index gave up after retries",
            attempts=self._max_attempts,
            error=self.state.error,
        )
        if self._on_failed is not None:
            try:
                await self._on_failed(self.state.error or "initial index failed")
            except Exception as exc:  # callback is best-effort, never mask the give-up
                _log.warning("index on_failed callback failed", error=str(exc))
