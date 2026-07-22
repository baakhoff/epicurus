"""Spine emitters for the knowledge module (#665) — doc events debounced, syncs batched.

Knowledge announces four kinds of world change on the module event spine (ADR-0103):

* ``knowledge.doc_created`` / ``knowledge.doc_deleted`` — emitted immediately at the
  change (editor save of a new file, file-tree delete, approved agent suggestion).
* ``knowledge.doc_updated`` — **debounced to settled saves.** The editor's auto-save
  (ADR-0042) fires a ``PUT …/doc`` on every idle pause, so one editing session is many
  saves; each (re)arms a per-doc quiet window (``KNOWLEDGE_EVENTS_DEBOUNCE_S``, default
  120s) and the event fires once when it passes untouched.
* ``knowledge.vault_synced`` — **one batch event per watcher pass** (#232): after an
  external change lands (Obsidian Sync) and the incremental re-index completes, a single
  event carries the pass's counts. A pass that changed nothing emits nothing — the
  watcher wakes on every debounced disk change, and a no-op pass is not news. The counts
  are the indexer's own (``indexed`` merges added+updated; the walk does not distinguish).
* ``knowledge.index_failed`` — rate-limited (``KNOWLEDGE_INDEX_FAILED_COOLDOWN_S``,
  default 900s, the mail.sync_failed posture): the initial index giving up after its
  retry budget, or a watcher pass failing. Per-save index misses are *not* spine events —
  the editor already surfaces them inline ("saved · not indexed") and the next save
  retries.

Every emission is best-effort: a spine hiccup is logged and never fails the change that
already landed. The debounce is the same sweeper-over-a-dict shape as notes'
(:mod:`epicurus_notes.events`) — duplicated deliberately rather than shared, since a
two-module helper does not yet earn a place in the high-contention core lib.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from epicurus_core import EntityRef, EventBus, emit_event, get_logger
from epicurus_knowledge.refs import KNOWLEDGE_KIND, SOURCE_NOTE, doc_title, encode_ref

log = get_logger("epicurus_knowledge.events")

MODULE = "knowledge"

DOC_CREATED = "knowledge.doc_created"
DOC_UPDATED = "knowledge.doc_updated"
DOC_DELETED = "knowledge.doc_deleted"
VAULT_SYNCED = "knowledge.vault_synced"
INDEX_FAILED = "knowledge.index_failed"

_SWEEP_INTERVAL_S = 1.0


@dataclass
class _PendingUpdate:
    """One document's coalesced, not-yet-emitted update."""

    path: str
    last_saved_at: datetime
    saves: int
    deadline: float


class KnowledgeEventEmitter:
    """Emits ``knowledge.*`` on the spine; updates debounced, failures rate-limited.

    Args:
        bus: The event spine. ``None`` disables emission entirely (tests / no NATS).
        tenant: The tenant every event is scoped to (constraint #1).
        debounce_s: Quiet window before a saved doc's ``doc_updated`` fires.
        failure_cooldown_s: Minimum gap between ``index_failed`` emissions — a vault
            stuck failing must not storm the bus once per watcher wake.
        clock: Monotonic time source, injectable for deterministic tests.
    """

    def __init__(
        self,
        bus: EventBus | None,
        *,
        tenant: str,
        debounce_s: float = 120.0,
        failure_cooldown_s: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bus = bus
        self._tenant = tenant
        self._debounce_s = debounce_s
        self._failure_cooldown_s = failure_cooldown_s
        self._clock = clock
        self._pending: dict[str, _PendingUpdate] = {}
        self._last_failure_at: float | None = None

    # ── doc change hooks (called by VaultPages) ──────────────────────────────

    async def doc_saved(self, path: str, *, created: bool) -> None:
        """Record one save. A brand-new doc announces itself now; an edit waits to settle."""
        if created:
            now = datetime.now(UTC)
            await self._emit(
                DOC_CREATED,
                dedup_key=f"{path}:created:{now.isoformat()}",
                payload={"path": path, "title": doc_title(path)[:200]},
                entity_ref=self._ref(path),
                occurred_at=now,
            )
            return
        pending = self._pending.get(path)
        saves = pending.saves + 1 if pending else 1
        self._pending[path] = _PendingUpdate(
            path=path,
            last_saved_at=datetime.now(UTC),
            saves=saves,
            deadline=self._clock() + self._debounce_s,
        )

    async def doc_deleted(self, path: str) -> None:
        """Announce a deletion now; a pending update for it is moot and dropped."""
        self._pending.pop(path, None)
        now = datetime.now(UTC)
        await self._emit(
            DOC_DELETED,
            dedup_key=f"{path}:deleted:{now.isoformat()}",
            payload={"path": path},
            entity_ref=self._ref(path),
            occurred_at=now,
        )

    def doc_moved(self, from_path: str, to_path: str) -> None:
        """Re-key pending updates across a move — files and whole folders alike.

        A move emits nothing in v1 (#665 names created/updated/deleted); this keeps an
        in-flight editing session's eventual ``doc_updated`` pointing at a path that
        still exists. A folder move re-keys everything pending under its prefix.
        """
        moved = [
            key for key in self._pending if key == from_path or key.startswith(from_path + "/")
        ]
        for key in moved:
            pending = self._pending.pop(key)
            new_key = to_path + key[len(from_path) :]
            pending.path = new_key
            self._pending[new_key] = pending

    def drop_prefix(self, prefix: str) -> None:
        """Forget pending updates under *prefix* (a deleted knowledge base, #340)."""
        for key in [k for k in self._pending if k.startswith(prefix)]:
            self._pending.pop(key, None)

    # ── batch + failure hooks (watcher / runner) ─────────────────────────────

    async def vault_synced(self, counts: dict[str, int]) -> None:
        """One event per watcher pass that changed anything (#232). No-op passes are silent."""
        indexed = int(counts.get("indexed", 0))
        deleted = int(counts.get("deleted", 0))
        if indexed == 0 and deleted == 0:
            return
        now = datetime.now(UTC)
        await self._emit(
            VAULT_SYNCED,
            dedup_key=f"vault-synced:{now.isoformat()}",
            payload={
                "indexed": indexed,  # added + updated — the walk does not distinguish
                "deleted": deleted,
                "unchanged": int(counts.get("unchanged", 0)),
            },
            entity_ref=None,
            occurred_at=now,
        )

    async def index_failed(self, error: str) -> None:
        """Announce an indexing failure, rate-limited so a stuck vault cannot storm.

        A cooldown, not a fire-once marker (the mail.sync_failed posture): the vault may
        keep failing pass after pass, and each failure is real — the operator just does
        not need telling on every watcher wake. Each emission that clears the cooldown is
        a fresh observation, so the dedup key is time-based.
        """
        now_monotonic = self._clock()
        if (
            self._last_failure_at is not None
            and now_monotonic - self._last_failure_at < self._failure_cooldown_s
        ):
            return
        self._last_failure_at = now_monotonic
        now = datetime.now(UTC)
        await self._emit(
            INDEX_FAILED,
            dedup_key=f"index-failed:{now.isoformat()}",
            payload={"error": error[:200]},
            entity_ref=None,
            occurred_at=now,
        )

    # ── the debounce sweep ───────────────────────────────────────────────────

    async def flush_due(self, now: float | None = None) -> int:
        """Emit every pending update whose quiet window has passed; returns how many."""
        instant = self._clock() if now is None else now
        due = [p for p in self._pending.values() if p.deadline <= instant]
        for pending in due:
            self._pending.pop(pending.path, None)
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

    def _ref(self, path: str) -> EntityRef:
        # The same resolvable ref shape knowledge_search cites (#143): the resolver
        # decodes (source, path) out of the opaque id and serves the hover-card.
        return EntityRef(
            ref_id=encode_ref(SOURCE_NOTE, path),
            module=MODULE,
            kind=KNOWLEDGE_KIND,
            title=doc_title(path)[:200],
        )

    async def _emit_update(self, pending: _PendingUpdate) -> None:
        await self._emit(
            DOC_UPDATED,
            dedup_key=f"{pending.path}:updated:{pending.last_saved_at.isoformat()}",
            payload={
                "path": pending.path,
                "title": doc_title(pending.path)[:200],
                "saves": pending.saves,
            },
            entity_ref=self._ref(pending.path),
            occurred_at=pending.last_saved_at,
        )

    async def _emit(
        self,
        event_type: str,
        *,
        dedup_key: str,
        payload: dict[str, object],
        entity_ref: EntityRef | None,
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
