"""Spine emitters for the notes module (#665) — content events, updates debounced.

Notes announces its world changes on the module event spine (ADR-0103):

* ``notes.note_created`` / ``notes.note_deleted`` — emitted immediately at the change.
* ``notes.note_updated`` — **debounced to settled saves.** The editor's auto-save
  (ADR-0042) fires a ``PUT …/doc`` on every idle pause and page-leave, so one editing
  session is many module-side saves. Each save (re)arms a per-note quiet window
  (``NOTES_EVENTS_DEBOUNCE_S``, default 120s); the event fires once, when the window
  passes with no further save — carrying the *last* save's timestamp as ``occurred_at``
  and the number of saves it coalesced.

Every emission is best-effort: a spine hiccup is logged and never fails the save or
delete that already landed (the same posture as mail's emitters, #663).

The debounce is a plain dict of pending entries swept by :meth:`NoteEventEmitter.run`;
due-ness is decided by :meth:`flush_due` against an injectable monotonic clock, so tests
drive it with constructed times instead of sleeping (the ADR-0092/0098 idiom). A
per-note timer task was rejected: cancel-and-respawn task lifecycles are exactly where
this codebase has grown ``CancelledError`` deadlocks before.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from epicurus_core import EntityRef, EventBus, emit_event, get_logger

log = get_logger("epicurus_notes.events")

MODULE = "notes"

NOTE_CREATED = "notes.note_created"
NOTE_UPDATED = "notes.note_updated"
NOTE_DELETED = "notes.note_deleted"

# How often the sweeper re-checks pending updates for settledness. The debounce window is
# measured in minutes, so a 1s tick costs nothing and keeps settle latency negligible.
_SWEEP_INTERVAL_S = 1.0


@dataclass
class _PendingUpdate:
    """One note's coalesced, not-yet-emitted update."""

    slug: str
    title: str
    last_saved_at: datetime  # the eventual event's occurred_at — when the change happened
    saves: int  # how many saves this window coalesced (feed signal, not content)
    deadline: float  # monotonic instant the window settles


class NoteEventEmitter:
    """Emits ``notes.*`` on the spine; ``note_updated`` debounced to settled saves.

    Args:
        bus: The event spine. ``None`` disables emission entirely — callers that only
            want the page logic (tests, tools) need no NATS connection.
        tenant: The tenant every event is scoped to (constraint #1).
        debounce_s: The quiet window a note must sit unsaved before ``note_updated``
            fires. The editor idle-saves every ~4s while a document is open (ADR-0042),
            so this is minutes, not milliseconds — one event per editing session.
        clock: Monotonic time source, injectable for deterministic tests.
    """

    def __init__(
        self,
        bus: EventBus | None,
        *,
        tenant: str,
        debounce_s: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bus = bus
        self._tenant = tenant
        self._debounce_s = debounce_s
        self._clock = clock
        self._pending: dict[str, _PendingUpdate] = {}

    # ── change hooks (called by NotesPages) ──────────────────────────────────

    async def note_saved(self, slug: str, title: str, *, created: bool) -> None:
        """Record one save. A brand-new note announces itself now; an edit waits to settle."""
        if created:
            now = datetime.now(UTC)
            await self._emit(
                NOTE_CREATED,
                dedup_key=f"{slug}:created:{now.isoformat()}",
                payload={"slug": slug, "title": title[:200]},
                entity_ref=self._ref(slug, title),
                occurred_at=now,
            )
            return
        pending = self._pending.get(slug)
        saves = pending.saves + 1 if pending else 1
        self._pending[slug] = _PendingUpdate(
            slug=slug,
            title=title,
            last_saved_at=datetime.now(UTC),
            saves=saves,
            deadline=self._clock() + self._debounce_s,
        )

    async def note_deleted(self, slug: str) -> None:
        """Announce a deletion now; a pending update for it is moot and dropped."""
        self._pending.pop(slug, None)
        now = datetime.now(UTC)
        await self._emit(
            NOTE_DELETED,
            dedup_key=f"{slug}:deleted:{now.isoformat()}",
            payload={"slug": slug},
            entity_ref=self._ref(slug, slug),
            occurred_at=now,
        )

    def note_moved(self, from_slug: str, to_slug: str) -> None:
        """Re-key a pending update so it settles under the note's new slug.

        A move itself emits nothing in v1 (#665 names created/updated/deleted only); this
        just keeps an in-flight editing session's eventual ``note_updated`` pointing at a
        slug that still exists.
        """
        pending = self._pending.pop(from_slug, None)
        if pending is not None:
            pending.slug = to_slug
            self._pending[to_slug] = pending

    # ── the debounce sweep ───────────────────────────────────────────────────

    async def flush_due(self, now: float | None = None) -> int:
        """Emit every pending update whose quiet window has passed; returns how many."""
        instant = self._clock() if now is None else now
        due = [p for p in self._pending.values() if p.deadline <= instant]
        for pending in due:
            # Pop before the await so a save landing mid-emit re-arms a fresh window
            # rather than racing this one.
            self._pending.pop(pending.slug, None)
            await self._emit_update(pending)
        return len(due)

    async def flush_all(self) -> int:
        """Emit everything pending regardless of settledness (shutdown path)."""
        pending = list(self._pending.values())
        self._pending.clear()
        for entry in pending:
            await self._emit_update(entry)
        return len(pending)

    async def run(self) -> None:
        """Sweep pending updates until cancelled — the lifespan's background task."""
        while True:
            await asyncio.sleep(_SWEEP_INTERVAL_S)
            await self.flush_due()

    # ── internals ────────────────────────────────────────────────────────────

    def _ref(self, slug: str, title: str) -> EntityRef:
        # Notes has no resolver (notes are private) — the ref still names the note so a
        # feed row shows a chip; the hover-card falls back to the ref's own title.
        return EntityRef(ref_id=slug, module=MODULE, kind="note", title=title[:200])

    async def _emit_update(self, pending: _PendingUpdate) -> None:
        await self._emit(
            NOTE_UPDATED,
            dedup_key=f"{pending.slug}:updated:{pending.last_saved_at.isoformat()}",
            payload={
                "slug": pending.slug,
                "title": pending.title[:200],
                "saves": pending.saves,
            },
            entity_ref=self._ref(pending.slug, pending.title),
            occurred_at=pending.last_saved_at,
        )

    async def _emit(
        self,
        event_type: str,
        *,
        dedup_key: str,
        payload: dict[str, object],
        entity_ref: EntityRef,
        occurred_at: datetime,
    ) -> None:
        """Fire one event, best-effort — a spine hiccup never fails the change it reports."""
        if self._bus is None:
            return
        try:
            await emit_event(
                self._bus,
                tenant_id=self._tenant,
                module=MODULE,
                event_type=event_type,
                dedup_key=dedup_key,
                payload=dict(payload),
                entity_ref=entity_ref,
                occurred_at=occurred_at,
            )
        except Exception as exc:
            # `event=` is structlog's reserved key for the message itself — use event_type.
            log.warning("spine emit failed", event_type=event_type, error=str(exc))
