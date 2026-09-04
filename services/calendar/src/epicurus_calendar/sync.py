"""Calendar's reconcile layer — external changes reach the event spine (#831).

Until this existed, calendar's `event_created`/`event_updated`/`event_cancelled` had exactly
one emitter: the router's **provider-write seam**. That seam sees every write made *through*
this module and, structurally, nothing else — an event created, moved or deleted in Google
Calendar's own UI was never observed and never announced. Every downstream consumer (the
automations matcher, push alerts, the event feed) was correct and simply never heard about
half the changes to the operator's calendar.

This is the missing observer, built on the shape mail's reconcile proved (ADR-0096, #623/#796):

- **Incremental, not a re-poll.** Each watched collection keeps a provider cursor (Google's
  ``syncToken``) in :mod:`epicurus_calendar.sync_store`. A tick asks "what changed since?" and
  gets back only the delta — usually nothing at all.
- **A lapsed cursor is a recoverable state, not a failure.** Google answers an expired token
  with ``410 GONE``; the seam reports that as ``None`` and this module full-syncs and **diffs
  against its own cache**, so the gap is reported exactly rather than replayed blindly or
  swallowed.
- **A first-ever sync is silent** (the no-firehose rule). A calendar you already had is not
  news; announcing every existing event the first time a collection is watched would hand the
  automations engine hundreds of triggers for nothing that happened.
- **It never double-announces a write we made ourselves.** See "Self-write suppression".
- **An idle deployment costs nothing and says nothing.** With no external calendar connected
  or enabled, a tick resolves zero targets and returns — no provider call, no log line. That
  is the #815 one-rule degrade doing its job: a ref whose provider is missing degrades to the
  local collection, and the local store is not a sync target (it has no "outside epicurus").

**Self-write suppression.** The write seam emits immediately (latency matters for an
automation) and the reconcile would see the very same change minutes later. So the write seam
records a short-lived marker in the self-write ledger — keyed
``"<event type>|<provider>:<id>"``, and for a series-scoped write the series id too — and this
module checks that ledger before every emission. An exact-id hit is *consumed* (so a genuinely
external change to the same event ten minutes later is announced normally); a series-id hit is
only *peeked* (one series-wide write comes back as one change per occurrence, and those
occurrences may straddle two passes — so the marker stays until its TTL expires it, #843). Which
of the two a decision gets is decided by the ``collapsed`` flag, not by comparing ids: collapsing
re-keys a group onto the series id, which makes ``event_id == series_id`` and would otherwise
send the *series* marker down the consume path. The key deliberately excludes the change hash
the ``dedup_key`` carries: suppression must survive the provider normalising content on the way
back, and must match across the series/occurrence identity shift. The races this accepts are
recorded in the ADR.

**No-firehose, second form.** A recurring series arrives from Google as one change per
occurrence. Emitting 30 ``event_created``s because someone added a weekly stand-up would be
precisely the firehose the spine's payload discipline exists to prevent, so changes are
collapsed per ``(series, event type)`` within a pass: one emission, keyed on the *series* when
more than one occurrence moved and on the occurrence itself when only one did. A whole pass is
additionally capped, mirroring mail's resume-backlog ceiling.

Polling, not push: Google's ``events.watch`` + a webhook channel would be lower-latency, but it
needs a publicly reachable HTTPS endpoint a local-first self-host cannot assume, and channels
expire and must be renewed — the same reasoning mail's poller records for Gmail's Pub/Sub push.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple, Protocol

from epicurus_calendar.providers.base import CalendarProvider, EventChange
from epicurus_calendar.spine import (
    EVENT_CANCELLED,
    EVENT_CREATED,
    EVENT_UPDATED,
    cancelled_dedup_key,
    created_dedup_key,
    event_change_hash,
    event_summary_payload,
    self_write_key,
    updated_dedup_key,
)
from epicurus_calendar.sync_store import CalendarSyncStore, SelfWriteLedger, SyncedEvent
from epicurus_core import CollectionRef, EntityRef, EventBus, emit_event, get_logger

log = get_logger("epicurus_calendar.sync")

DEFAULT_POLL_INTERVAL_S = 300.0
"""How often the reconcile loop ticks.

Matched to mail's poller rather than to calendar's own lead-time scheduler (60s): a tick is one
delta call per watched calendar, cheap but not free, and "someone edited a meeting" is not
minute-critical the way "a meeting starts in fifteen minutes" is. Five minutes bounds the
worst-case latency of noticing an external change at five minutes while leaving a calendar
nobody touches costing ~288 delta calls a day per collection — well inside Google's quota.
"""

DEFAULT_POLL_JITTER_FRAC = 0.1
"""Fraction of the interval to randomise each sleep by (±10%).

Calendar now runs two periodic loops in one process (this and the lead-time scheduler). Left
exactly periodic they drift into lockstep with each other and, in a multi-tenant deployment,
across tenants — bunching provider calls into synchronised bursts. A little jitter is the
cheapest fix; ``0`` disables it and makes a test's timing exactly predictable.
"""

DEFAULT_WINDOW_DAYS = 30
"""How far back a first sync anchors its window (Google binds it to the minted cursor).

Not "all history": priming a decade of past events would cost a long pagination walk and fill
the cache with rows no consumer will ever ask about. Thirty days back is enough that a
just-ended or in-progress event is still watched, and everything forward of *now* is unbounded
regardless — the anchor is a ``timeMin`` only.
"""

DEFAULT_MAX_EMISSIONS_PER_PASS = 50
"""Ceiling on how many events one collection's pass may announce (mirrors mail's #796 cap).

A bound on the notification burst, never on the sync itself: the cache is updated in full
either way, so a truncated pass does not re-announce the shortfall on the next tick — it stays
unannounced, and the truncation is logged so it is visible rather than assumed. Fifty is far
past any real editing session and small enough that a bulk import into the connected calendar
cannot hand the automations engine a thousand triggers at once.
"""


class SyncTargetSource(Protocol):
    """Supplies the collections to reconcile (the module's ``CollectionRouter``)."""

    async def sync_targets(
        self, *, tenant_id: str
    ) -> list[tuple[CalendarProvider, CollectionRef]]: ...


class _Decision(NamedTuple):
    """One classified change, before series-collapsing and suppression."""

    event_type: str
    event_id: str
    series_id: str | None
    title: str
    payload: dict[str, Any]
    start: datetime
    # Set by :func:`_collapse` when several occurrences of one series were folded into this
    # single emission and it was therefore re-keyed onto the **series** id. It is the only way
    # :meth:`CalendarReconciler._suppressed` can still tell a series id from an occurrence id
    # afterwards — and the difference decides whether the ledger marker is peeked or consumed
    # (#843). Never set by :func:`_classify`: a change straight off the provider is always about
    # the id it names.
    collapsed: bool = False


class CalendarReconciler:
    """Incremental sync over every watched collection, emitting what the write seam can't see.

    Args:
        targets: Resolves which collections to watch — the router, which applies the operator's
            selection and the #815 one-rule degrade.
        store: Durable cursors + the observed-event cache.
        ledger: The self-write ledger. ``None`` disables suppression entirely (a caller that
            only wants observation, e.g. a unit test with no write path).
        bus: The event spine. ``None`` skips emission — a reconcile that only warms the cache.
        tenant_id: The tenant this instance serves (constraint #1).
        window_days: How far back a first/resync anchors its window.
        max_emissions_per_pass: Per-collection ceiling on one pass's emissions.
    """

    def __init__(
        self,
        *,
        targets: SyncTargetSource,
        store: CalendarSyncStore,
        tenant_id: str,
        ledger: SelfWriteLedger | None = None,
        bus: EventBus | None = None,
        window_days: int = DEFAULT_WINDOW_DAYS,
        max_emissions_per_pass: int = DEFAULT_MAX_EMISSIONS_PER_PASS,
    ) -> None:
        self._targets = targets
        self._store = store
        self._ledger = ledger
        self._bus = bus
        self._tenant = tenant_id
        self._window_days = window_days
        self._max_emissions = max_emissions_per_pass
        # Single-flight, exactly as mail's cache takes for its two reconcile callers (#796):
        # the whole read-cursor → fetch-delta → emit → advance-cursor sequence must not
        # interleave with itself, or two overlapping passes observe the same pre-advance cursor
        # and announce one change twice. Per process, like the tasks scheduler's claim — two
        # replicas would each hold their own, which is out of scope while modules are
        # single-instance, and the spine's dedup_key is the backstop for even that.
        self._lock = asyncio.Lock()

    async def reconcile(self) -> int:
        """One full pass over every watched collection. Returns how many events it announced.

        A collection that fails is logged and skipped, never aborting the others (#209): one
        calendar with a revoked grant must not stop a second, healthy one from being watched.
        """
        async with self._lock:
            targets = await self._targets.sync_targets(tenant_id=self._tenant)
            if not targets:
                return 0
            if self._ledger is not None:
                await self._ledger.prune(tenant=self._tenant)
            emitted = 0
            for provider, ref in targets:
                try:
                    emitted += await self._sync_collection(provider, ref)
                except Exception as exc:
                    log.warning(
                        "calendar reconcile failed for a collection; skipping (#209)",
                        account=ref.account,
                        collection=ref.collection,
                        error=str(exc),
                    )
            return emitted

    # ── per-collection sync ──────────────────────────────────────────────────

    async def _sync_collection(self, provider: CalendarProvider, ref: CollectionRef) -> int:
        """Advance one collection: incremental when we hold a usable cursor, else full."""
        state = await self._store.get_state(
            tenant=self._tenant, account=ref.account, collection=ref.collection
        )
        if state is None:
            return await self._full_sync(provider, ref, first=True)
        if not state.sync_token:
            # Primed, but the last full sync could not mint a cursor. Diff again rather than
            # wait for one — the cache makes that exact, not a re-announcement of everything.
            return await self._full_sync(provider, ref, first=False)
        page = await provider.changed_events_since(
            tenant_id=self._tenant,
            calendar_id=ref.collection or None,
            cursor=state.sync_token,
        )
        if page is None:
            log.info(
                "calendar sync cursor expired; falling back to a full resync (#831)",
                tenant=self._tenant,
                account=ref.account,
                collection=ref.collection,
            )
            return await self._full_sync(provider, ref, first=False)
        emitted = await self._apply(provider, ref, page.changes)
        await self._store.set_state(
            tenant=self._tenant,
            account=ref.account,
            collection=ref.collection,
            sync_token=page.next_cursor,
            window_start=state.window_start,
        )
        return emitted

    async def _full_sync(
        self, provider: CalendarProvider, ref: CollectionRef, *, first: bool
    ) -> int:
        """Re-read the window and mint a new cursor.

        *first* is the whole no-firehose decision, and it comes from the sync-state row's mere
        existence rather than from anything about the cursor: a never-synced collection primes
        the cache in **silence**, while a collection that was synced before and lost the thread
        has a real gap, which the cache lets us report exactly — creations, edits and
        cancellations, not "here is everything again".

        A cached row that merely fell out of the *new* window (the anchor moves forward on each
        resync) is pruned silently. Treating it as a cancellation would announce the passage of
        time as an operator action.
        """
        since = datetime.now(UTC) - timedelta(days=self._window_days)
        page = await provider.full_sync(
            tenant_id=self._tenant, calendar_id=ref.collection or None, since=since
        )
        live = [c for c in page.changes if not c.cancelled and c.event is not None]
        if first:
            await self._store.replace_events(
                tenant=self._tenant,
                account=ref.account,
                collection=ref.collection,
                events=[_observed(change) for change in live],
            )
            log.info(
                "calendar collection primed; first sync announces nothing (#831)",
                tenant=self._tenant,
                account=ref.account,
                collection=ref.collection,
                events=len(live),
            )
            emitted = 0
        else:
            cached = await self._store.get_events(
                tenant=self._tenant, account=ref.account, collection=ref.collection
            )
            present = {change.event_id for change in live}
            # A full listing says what *is*; a delta has to also say what stopped being. An id
            # we cached, whose event still overlaps the window, and which the listing no longer
            # mentions was deleted while we could not watch — so it becomes a tombstone here
            # and travels the same classification path a provider-reported one does.
            gone = [
                EventChange(event_id=event_id, cancelled=True, series_id=row.series_id)
                for event_id, row in cached.items()
                if event_id not in present and row.end > since
            ]
            emitted = await self._apply(provider, ref, [*page.changes, *gone], cached=cached)
            # Anything absent that ended before the *new* anchor merely fell out of the window
            # (which moves forward on every resync). Announcing that would report the passage
            # of time as an operator action, so it is pruned in silence. The window test is the
            # provider's own — Google's ``timeMin`` bounds an event's *end*, so a long event
            # that started before the anchor is still legitimately live.
            aged_out = [
                event_id
                for event_id, row in cached.items()
                if event_id not in present and row.end <= since
            ]
            if aged_out:
                await self._store.remove_events(
                    tenant=self._tenant,
                    account=ref.account,
                    collection=ref.collection,
                    event_ids=aged_out,
                )
        await self._store.set_state(
            tenant=self._tenant,
            account=ref.account,
            collection=ref.collection,
            sync_token=page.next_cursor,
            window_start=since,
        )
        if page.next_cursor is None:
            log.warning(
                "calendar full sync returned no cursor; next pass will full-sync again (#831)",
                tenant=self._tenant,
                account=ref.account,
                collection=ref.collection,
            )
        return emitted

    # ── classification + emission ────────────────────────────────────────────

    async def _apply(
        self,
        provider: CalendarProvider,
        ref: CollectionRef,
        changes: Sequence[EventChange],
        *,
        cached: dict[str, SyncedEvent] | None = None,
    ) -> int:
        """Classify a delta against the cache, update the cache, announce what is news.

        *cached* lets the resync path hand in the snapshot it already had to load, so one pass
        reads the cache once.
        """
        if not changes:
            return 0
        if cached is None:
            cached = await self._store.get_events(
                tenant=self._tenant, account=ref.account, collection=ref.collection
            )
        decisions: list[_Decision] = []
        upserts: list[SyncedEvent] = []
        removals: list[str] = []
        for change in changes:
            decision = _classify(change, cached.get(change.event_id))
            if change.cancelled:
                if change.event_id in cached:
                    removals.append(change.event_id)
            elif change.event is not None:
                upserts.append(_observed(change))
            if decision is not None:
                decisions.append(decision)
        # The cache is written first and in full, so a truncated or failed emission never
        # re-announces itself on the next tick — at-least-once on the spine, exactly-once in
        # the cache.
        await self._store.upsert_events(
            tenant=self._tenant, account=ref.account, collection=ref.collection, events=upserts
        )
        await self._store.remove_events(
            tenant=self._tenant, account=ref.account, collection=ref.collection, event_ids=removals
        )
        return await self._emit_all(provider, ref, decisions)

    async def _emit_all(
        self, provider: CalendarProvider, ref: CollectionRef, decisions: Sequence[_Decision]
    ) -> int:
        """Collapse per series, drop our own writes, and announce what is left."""
        emitted = 0
        for group in _collapse(decisions):
            if emitted >= self._max_emissions:
                log.warning(
                    "calendar reconcile truncated its announcements for this pass (#831)",
                    tenant=self._tenant,
                    account=ref.account,
                    collection=ref.collection,
                    limit=self._max_emissions,
                )
                break
            if await self._suppressed(provider.name, group):
                continue
            if await self._emit(provider.name, group):
                emitted += 1
        return emitted

    async def _suppressed(self, provider_name: str, decision: _Decision) -> bool:
        """Whether this module already announced *decision* at the provider-write seam.

        Two shapes, and which one applies is decided by *how the decision got its id* — not by
        comparing ids, which is the trap #843 records. A **collapsed** decision has been re-keyed
        onto the series id by :func:`_collapse`, so its ``event_id`` *is* a series id and its
        marker is only **peeked**: one series-scoped write legitimately matches many occurrences,
        and those occurrences can straddle two passes (the delta paginates, an occurrence is
        edited later in the window, a restart splits the feed). Consuming it there — which is
        what a plain exact-id comparison did, because collapsing makes ``event_id ==
        series_id`` — deletes the marker on the first pass and re-announces the same write on the
        second.

        Anything not collapsed is a genuine single-occurrence match and is **consumed**: once a
        write's marker has done its job, a later, genuinely external change to the same event
        must be announced normally. A single occurrence of a series falls back to peeking the
        series marker when it carries no marker of its own.

        A peeked series marker is never released by a read — it expires on its own TTL
        (``SYNC_SELF_WRITE_TTL_S``, 15 minutes; pruned once per pass). There is no "the series
        write is fully observed" moment to release it at: nothing tells us how many occurrences
        one series write will come back as, so an explicit release would have to guess, and
        guessing low is exactly the re-announcement this fixes.
        """
        if self._ledger is None:
            return False
        key = self_write_key(decision.event_type, provider_name, decision.event_id)
        if decision.collapsed:
            return await self._ledger.peek(tenant=self._tenant, key=key)
        if await self._ledger.consume(tenant=self._tenant, key=key):
            return True
        series_id = decision.series_id
        if series_id is None or series_id == decision.event_id:
            return False
        return await self._ledger.peek(
            tenant=self._tenant,
            key=self_write_key(decision.event_type, provider_name, series_id),
        )

    async def _emit(self, provider_name: str, decision: _Decision) -> bool:
        """Publish one decision; ``True`` if something went out."""
        if self._bus is None:
            return False
        if decision.event_type == EVENT_CANCELLED:
            dedup_key = cancelled_dedup_key(provider_name, decision.event_id)
        elif decision.event_type == EVENT_UPDATED:
            dedup_key = updated_dedup_key(
                provider_name, decision.event_id, str(decision.payload["change_hash"])
            )
        else:
            dedup_key = created_dedup_key(provider_name, decision.event_id)
        payload = {k: v for k, v in decision.payload.items() if k != "change_hash"}
        try:
            await emit_event(
                self._bus,
                tenant_id=self._tenant,
                module="calendar",
                event_type=decision.event_type,
                dedup_key=dedup_key,
                payload=payload,
                entity_ref=EntityRef(
                    ref_id=decision.event_id,
                    module="calendar",
                    kind="event",
                    title=decision.title,
                ),
            )
        except Exception as exc:  # a spine hiccup must never cost the cache write already made
            log.warning(
                f"{decision.event_type} emit failed (reconcile)",
                event_id=decision.event_id,
                error=str(exc),
            )
            return False
        return True


def _observed(change: EventChange) -> SyncedEvent:
    """The cache row for a live change (never called for a tombstone)."""
    event = change.event
    assert event is not None  # guarded by every caller
    return SyncedEvent(
        event_id=change.event_id,
        series_id=change.series_id or event.recurring_event_id,
        title=event.title,
        start=event.start,
        end=event.end,
        all_day=event.all_day,
        change_hash=event_change_hash(event),
    )


def _classify(change: EventChange, cached: SyncedEvent | None) -> _Decision | None:
    """What (if anything) this change is news about, given what we last observed.

    Three real outcomes and one non-outcome. A tombstone for an id we never cached is *not* a
    cancellation to announce — we never said it existed, and Google's ``showDeleted`` feed is
    full of occurrences that were cancelled before we ever looked. An event whose change hash
    is unmoved is likewise nothing: the provider mentioned it (a field we do not track moved,
    or the delta simply overlapped), which is not a change to the event as the spine defines
    one.
    """
    if change.cancelled:
        if cached is None:
            return None
        return _Decision(
            event_type=EVENT_CANCELLED,
            event_id=change.event_id,
            series_id=change.series_id or cached.series_id,
            title=cached.title,
            payload={"title": cached.title[:200]},
            start=cached.start,
        )
    event = change.event
    if event is None:
        return None
    new_hash = event_change_hash(event)
    series_id = change.series_id or event.recurring_event_id
    payload = event_summary_payload(event)
    payload["change_hash"] = new_hash
    if cached is None:
        return _Decision(
            event_type=EVENT_CREATED,
            event_id=change.event_id,
            series_id=series_id,
            title=event.title,
            payload=payload,
            start=event.start,
        )
    if cached.change_hash == new_hash:
        return None
    payload["time_changed"] = cached.start != event.start or cached.end != event.end
    return _Decision(
        event_type=EVENT_UPDATED,
        event_id=change.event_id,
        series_id=series_id,
        title=event.title,
        payload=payload,
        start=event.start,
    )


def _collapse(decisions: Sequence[_Decision]) -> list[_Decision]:
    """One emission per ``(series, event type)`` — the recurring-series no-firehose rule.

    A series-wide action (created, rescheduled, deleted) reaches us as one change per
    occurrence. Announcing each would turn a single operator action into dozens of spine
    events, so a group collapses to its earliest-starting member, re-keyed onto the **series**
    id — which is also the id the write seam used, so suppression lines up. A group of one is
    left exactly as it was, keyed on the occurrence, because that is what actually changed.

    The cost, stated plainly: two occurrences of one series edited differently inside a single
    pass yield one event, not two. Both cache rows are still updated, so the quiet one is not
    re-announced later — it is simply not announced. That trade is deliberate; the alternative
    is an unbounded burst for a single click in Google's UI.

    A re-keyed group is flagged ``collapsed`` so suppression can still tell what its id means
    (#843). The flag is set only when the group key really came from a *series* id: a group that
    formed on a repeated occurrence id (one delta mentioning the same event twice) is keyed on
    that event's own id, and its ledger marker is an ordinary exact-id one to consume.
    """
    grouped: dict[tuple[str, str], list[_Decision]] = {}
    for decision in decisions:
        key = (decision.series_id or decision.event_id, decision.event_type)
        grouped.setdefault(key, []).append(decision)
    collapsed: list[_Decision] = []
    for (group_key, _), group in grouped.items():
        first = min(group, key=lambda d: d.start)
        if len(group) == 1:
            collapsed.append(first)
        else:
            series_scoped = any(d.series_id == group_key for d in group)
            collapsed.append(
                first._replace(event_id=group_key, series_id=group_key, collapsed=series_scoped)
            )
    collapsed.sort(key=lambda d: (d.start, d.event_id))
    return collapsed


async def tick(*, reconciler: CalendarReconciler) -> int:
    """One poll pass. Returns how many events it announced (``0`` for an idle deployment)."""
    return await reconciler.reconcile()


async def run_periodic(
    *,
    reconciler: CalendarReconciler,
    tenant: str,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    jitter_frac: float = DEFAULT_POLL_JITTER_FRAC,
) -> None:
    """Reconcile every *poll_interval_s* seconds (±jitter), forever.

    Ticks first and sleeps after, like mail's poller and calendar's own lead-time scheduler: a
    restart should notice what changed while the service was down straight away rather than
    after a full interval — and a resumed sync is precisely where that gap is reported.

    A non-positive *poll_interval_s* disables the loop and returns immediately: an operator who
    turns reconcile off gets no background task at all, not one that spins. One bad tick (an
    expired grant, a provider hiccup) is logged and skipped, never kills the loop — and repeats
    log at debug, so an account that stays broken produces one warning plus a recovery line
    rather than a warning every interval. Cancellation propagates untouched, so app shutdown
    stops the loop at its sleep rather than waiting one out.
    """
    if poll_interval_s <= 0:
        log.info("calendar reconcile disabled", tenant=tenant)
        return
    log.info("calendar reconcile started", tenant=tenant, interval_s=poll_interval_s)
    failing = False
    while True:
        try:
            await tick(reconciler=reconciler)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if failing:
                log.debug("calendar reconcile still failing", tenant=tenant, error=str(exc))
            else:
                log.warning("calendar reconcile failed", tenant=tenant, error=str(exc))
                failing = True
        else:
            if failing:
                log.info("calendar reconcile recovered", tenant=tenant)
                failing = False
        await asyncio.sleep(_next_sleep(poll_interval_s, jitter_frac))


def _next_sleep(poll_interval_s: float, jitter_frac: float) -> float:
    """The next sleep, jittered by ±*jitter_frac* and never negative."""
    if jitter_frac <= 0:
        return poll_interval_s
    spread = poll_interval_s * min(jitter_frac, 1.0)
    return max(0.0, poll_interval_s + random.uniform(-spread, spread))
