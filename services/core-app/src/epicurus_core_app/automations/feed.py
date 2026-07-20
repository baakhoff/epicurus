"""The automation runs feed (#669) — replay recent ledger entries, then tail live ones.

The observability page's second live surface, beside the raw events feed. Same contract
as :meth:`~epicurus_core_app.event_log.EventIntake.stream` (ADR-0031's shape): a
newly-opened feed replays recent history oldest-first, then yields live entries as the
runner records them; a subscriber queue is registered *before* the history query so an
entry landing mid-replay is queued rather than missed (the client de-duplicates on
``id`` — a duplicate row is cosmetic, a missing one is a correctness problem).

The feed holds no state of its own beyond live subscribers: the ledger
(``automation_runs``) is the copy of record, and this only saves the browser from
polling it. The runner hands every recorded run to :meth:`publish` via its
``on_recorded`` hook — skips included, which is the point: a rate-capped or paused run
being *visible* is why the tab exists.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from epicurus_core_app.automations.model import AutomationRun
from epicurus_core_app.automations.store import AutomationStore

# How many past runs a newly-opened feed replays before going live — sized like the
# events feed's history (FEED_HISTORY = 200): enough to see what just happened, small
# enough that opening the tab is one quick query.
FEED_HISTORY = 200

# Bound on the live fan-out queue per subscriber; past this, a tab that stopped reading
# drops its oldest pending runs (the feed is a tail, not a ledger — the table is).
_SUBSCRIBER_QUEUE_MAX = 500

_OUTCOMES = ("ok", "error", "skipped")


def valid_outcome(value: str) -> bool:
    """Whether *value* is one of the ledger's outcome states."""
    return value in _OUTCOMES


class RunFeed:
    """Fans freshly-recorded runs out to live feed subscribers.

    Knows nothing about HTTP; the routes layer turns :meth:`stream` into SSE frames.
    """

    def __init__(self, store: AutomationStore) -> None:
        self._store = store
        self._subscribers: list[asyncio.Queue[AutomationRun]] = []

    async def publish(self, run: AutomationRun) -> None:
        """Hand a just-recorded run to every live subscriber (the runner's hook)."""
        for queue in self._subscribers:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(run)

    async def stream(
        self,
        *,
        tenant: str,
        automation_id: str | None = None,
        outcome: str | None = None,
    ) -> AsyncIterator[AutomationRun]:
        """Replay recent runs (oldest first), then yield live ones as they are recorded.

        Filters apply server-side to history and live entries alike, so the client never
        receives a run it would throw away. Mirrors the events feed's 1-second poll on
        the live queue, which lets the caller notice a closed browser connection instead
        of blocking forever on an idle runner.
        """

        def _matches(run: AutomationRun) -> bool:
            return (
                run.tenant == tenant
                and (not automation_id or run.automation_id == automation_id)
                and (not outcome or run.outcome == outcome)
            )

        queue: asyncio.Queue[AutomationRun] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAX)
        self._subscribers.append(queue)
        try:
            history = await self._store.runs(
                tenant=tenant,
                automation_id=automation_id,
                outcome=outcome,
                limit=FEED_HISTORY,
            )
            for run in reversed(history):  # runs() is newest-first; a feed reads oldest-first
                yield run

            while True:
                try:
                    run = await asyncio.wait_for(queue.get(), timeout=1.0)
                except TimeoutError:
                    # Nothing pending — yield control so the caller can notice a disconnect.
                    await asyncio.sleep(0)
                    continue
                if _matches(run):
                    yield run
        finally:
            with contextlib.suppress(ValueError):
                self._subscribers.remove(queue)


__all__ = ["FEED_HISTORY", "RunFeed", "valid_outcome"]
