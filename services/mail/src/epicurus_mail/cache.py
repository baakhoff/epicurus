"""Cache-first mailbox orchestration (ADR-0096, #623).

:class:`CachedMailbox` sits between the mailbox page reads and the provider, backed by the
tenant-scoped :class:`~epicurus_mail.db.MailCache`. It gives the landing view two speeds:

- :meth:`landing` — the **instant** path. Serves the cached rows + rail with no provider
  call (a cold cache falls through to a one-time full sync). This is what makes the *second*
  open of Mail render in ~a second instead of fanning out ~28 Gmail calls.
- :meth:`reconcile` — the **background** path. Asks the provider (via the neutral change
  cursor) what changed since the last sync and patches only those rows, so new/changed
  messages and flag flips appear without a manual refresh and without a full refetch.
- :meth:`categories` — the Inbox's **category tabs** (#765), on their own short TTL. Assembling
  them is a provider fan-out (per category: is it populated, its newest message, its unread
  count), so they are cached rather than rebuilt per render, and dropped whenever our own
  mark-read invalidates a count.

Search and deeper (``cursor``) pages stay live — the cache only accelerates the default
landing view, which is the dogfood pain ("Mail takes far too long to open"). The
orchestrator is provider-neutral: it drives everything through the :class:`MailProvider`
seam, so an IMAP backend reuses it unchanged.

:meth:`reconcile` is also where ``mail.received`` and ``mail.sync_failed`` are emitted
(#663) — the one place a genuinely-new message or a broken sync is already known, rather
than duplicating that knowledge at every provider implementation. A **first-ever** sync never
emits ``mail.received`` (the no-firehose rule): it has no delta to report new-vs-seen against,
so treating a first load as "N new messages" would be noise, not news.

Since #796 those two read paths have **two** callers — the mailbox page and the background
poller (:mod:`epicurus_mail.poller`) — which is what makes the rest of this module's shape
load-bearing:

- **Single-flight.** Both entry points serialize on one per-instance lock, so a poll tick and
  a page reconcile can never interleave their cursor read/advance and emit the same message
  twice. The loser re-reads the cursor the winner just advanced and finds nothing new.
- **A resumed sync is not a first sync.** A full sync that follows an earlier successful one —
  the cursor lapsed while the service was down — replays ``mail.received`` for the window it
  missed (bounded by :data:`DEFAULT_RESUME_BACKLOG_LIMIT`) instead of swallowing it. Only a
  mailbox that has never been synced at all stays silent.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel, Field

from epicurus_core import EntityRef, EventBus, emit_event, get_logger
from epicurus_mail.db import MailCache
from epicurus_mail.provider import MailCategory, MailLabel, MailProvider, MailThreadSummary

log = get_logger("epicurus_mail.cache")

DEFAULT_RESUME_BACKLOG_LIMIT = 50
"""How many missed messages a *resumed* full sync replays as ``mail.received`` (#796).

A ceiling on the notification burst after an outage, not on the sync itself: the mailbox state
is restored in full either way, this only bounds how many events the gap can turn into. Fifty
is roughly "a long weekend of real mail" — enough that a normal restart replays everything,
small enough that a fortnight offline can't hand the automations engine a thousand triggers at
once. A truncated replay is logged, so the shortfall is visible rather than assumed.
"""


class LandingBundle(BaseModel):
    """The landing view's data: the rail, one page of rows, and the "Older" token (ADR-0096)."""

    labels: list[MailLabel] = Field(default_factory=list)
    threads: list[MailThreadSummary] = Field(default_factory=list)
    next_cursor: str | None = None


class CachedMailbox:
    """Cache-first landing + incremental reconcile over a :class:`MailProvider` (ADR-0096).

    Args:
        provider: The active mail backend (Gmail today).
        cache: The tenant-scoped cache store.
        tenant_id: The tenant this instance serves (constraint #1).
        default_label: The folder whose unread count backs the nav badge (Inbox).
        page_size: How many threads a full sync fetches from the provider.
        landing_size: How many rows the landing view returns from cache.
        bus: The event spine (#663). ``None`` skips emission entirely — a caller that only
            wants cache reads (tests, a manifest-only build) needs no NATS connection.
        provider_name: This instance's provider identity for event payloads (``"gmail"``
            today; a future IMAP provider passes its own).
        sync_failed_cooldown_s: Minimum gap between ``mail.sync_failed`` emissions for this
            instance — every mailbox page open can trigger a reconcile, so an account stuck
            failing must not storm the bus once per open.
        category_ttl_s: How long an assembled category-tab set stays fresh (#765). Assembling
            it is a provider fan-out, so it must not run per render; short enough that newly
            arrived mail moves the badges without anyone asking.
        resume_backlog_limit: How many missed messages a *resumed* full sync replays as
            ``mail.received`` (#796) — see :data:`DEFAULT_RESUME_BACKLOG_LIMIT`. ``0`` turns
            the replay off entirely (a resumed sync then behaves like a first-ever one).
    """

    def __init__(
        self,
        provider: MailProvider,
        cache: MailCache,
        *,
        tenant_id: str,
        default_label: str = "INBOX",
        page_size: int = 25,
        landing_size: int = 25,
        bus: EventBus | None = None,
        provider_name: str = "gmail",
        sync_failed_cooldown_s: float = 900.0,
        category_ttl_s: float = 60.0,
        resume_backlog_limit: int = DEFAULT_RESUME_BACKLOG_LIMIT,
    ) -> None:
        self._provider = provider
        self._cache = cache
        self._tenant = tenant_id
        self._default_label = default_label
        self._page_size = page_size
        self._landing_size = landing_size
        self._bus = bus
        self._provider_name = provider_name
        self._sync_failed_cooldown_s = sync_failed_cooldown_s
        self._last_sync_failed_at: float | None = None
        self._category_ttl_s = category_ttl_s
        self._resume_backlog_limit = resume_backlog_limit
        # Single-flight for everything that reads-then-advances the change cursor (#796). The
        # mailbox page and the background poller are two concurrent callers of the same cursor;
        # without this they can both observe the pre-advance value and emit `mail.received`
        # twice for one message. Instance-scoped, which is single-flight per account per
        # process — see `reconcile` for the multi-replica caveat.
        self._sync_lock = asyncio.Lock()

    # ── read paths ───────────────────────────────────────────────────────────

    async def landing(self, label: str) -> LandingBundle:
        """The instant landing view: cached rows + rail, or a one-time full sync when cold.

        The warm path never touches the sync lock — serving 25 cached rows must stay instant
        even while a poll tick is mid-flight. Only the cold path takes it, and re-checks the
        cache once it holds it: with a poller running, "cache is cold" is a racy observation,
        and two concurrent full syncs would double a ~28-call provider fan-out for one result.
        """
        if await self._cache.has_landing(tenant_id=self._tenant, label=label):
            return await self._bundle_from_cache(label)
        async with self._sync_lock:
            if await self._cache.has_landing(tenant_id=self._tenant, label=label):
                return await self._bundle_from_cache(label)
            return await self._full_sync(label)

    async def reconcile(self, label: str) -> LandingBundle:
        """Pull the delta since the last sync into the cache, then return the fresh landing.

        Cheap when idle: one ``changed_threads_since`` call that returns an empty delta just
        advances the cursor and re-serves the cache. When threads changed, only those rows are
        rebuilt (a single ``get_thread_summary`` each) and the rail's unread counts refreshed.
        A cold or expired cursor falls back to a full sync — which emits ``mail.received`` only
        when it is *resuming* a mailbox that was synced before (see :meth:`_full_sync`).

        **Single-flight (#796).** Two callers now reach here: the mailbox page (``?reconcile=1``)
        and the background poller. The whole read-cursor → fetch-delta → emit → advance-cursor
        sequence runs under one lock, so an overlapping run cannot see the same pre-advance
        cursor and emit a second ``mail.received`` for one message; it waits, re-reads the
        advanced cursor, and finds nothing new. The guard is **per process**, exactly like the
        tasks scheduler's ``_claim_materialize``: two replicas of the mail module against one
        mailbox would each hold their own lock. That is out of scope by the same reasoning —
        modules are single-instance today — and the spine's ``dedup_key`` (the provider message
        id) is the backstop that keeps even that case from reaching a consumer twice.

        A provider failure (``httpx.HTTPError`` — an auth failure surfaces this way, via
        ``PlatformClient.get_oauth_token``) emits ``mail.sync_failed`` and re-raises, so the
        existing HTTP-level error mapping (403 scope hints, 429 throttling) is unchanged.
        """
        async with self._sync_lock:
            return await self._reconcile_locked(label)

    async def _reconcile_locked(self, label: str) -> LandingBundle:
        """:meth:`reconcile`'s body, with the sync lock already held."""
        cursor = await self._cache.get_cursor(tenant_id=self._tenant)
        if cursor.is_empty():
            return await self._full_sync(label)
        try:
            changes = await self._provider.changed_threads_since(cursor)
            if changes is None:  # cursor too old to replay → full resync
                log.info("mail cursor expired; falling back to full resync", tenant=self._tenant)
                await self._emit_sync_failed(reason="cursor_expired")
                return await self._full_sync(label)
            if changes.changed_thread_ids:
                await self._apply_changes(label, changes.changed_thread_ids)
                labels = await self._provider.list_labels(count_ids=self._count_ids(label))
                await self._cache.replace_labels(tenant_id=self._tenant, labels=labels)
            if changes.new_message_ids:
                await self._emit_received(label, changes.new_message_ids)
            await self._cache.set_cursor(tenant_id=self._tenant, cursor=changes.next_cursor)
            return await self._bundle_from_cache(label)
        except httpx.HTTPError as exc:
            log.warning("mail reconcile failed", tenant=self._tenant, error=str(exc))
            await self._emit_sync_failed(reason="provider_error")
            raise

    async def categories(self, label: str) -> list[MailCategory]:
        """The Inbox's category tabs, briefly cached (#765).

        A cache hit costs one small local query; a miss (cold, expired, or invalidated by our
        own mark-read) asks the provider and stores the result — including an **empty**
        result, so an uncategorized mailbox doesn't re-probe the provider on every render.

        A provider failure is swallowed to an empty strip rather than raised: the tabs are an
        enhancement over the thread list, and a page that renders its mail without tabs is
        strictly better than a page that renders nothing. The failure is logged, and the
        underlying breakage still surfaces through the list read itself, which does raise.
        """
        cached = await self._cache.get_categories(
            tenant_id=self._tenant, label=label, max_age_s=self._category_ttl_s
        )
        if cached is not None:
            return cached
        try:
            categories = await self._provider.list_categories(label=label)
        except httpx.HTTPError as exc:
            log.warning("mail category tabs unavailable", tenant=self._tenant, error=str(exc))
            return []
        await self._cache.replace_categories(
            tenant_id=self._tenant, label=label, categories=categories
        )
        return categories

    # ── event spine (#663) ──────────────────────────────────────────────────

    async def _emit_received(self, label: str, message_ids: set[str]) -> None:
        """Emit ``mail.received`` for each genuinely-new message.

        One provider read per message, for message-accurate from/subject/attachments — the
        thread summary already fetched by :meth:`_apply_changes` reflects only that thread's
        *latest* message (:func:`~epicurus_mail.gmail._thread_summary`), which is wrong the
        moment more than one new message lands in the same thread within one reconcile window.
        A message that fails to fetch (deleted between detection and this read) is skipped and
        logged rather than failing the reconcile that already landed the cache write.
        """
        if self._bus is None:
            return
        for message_id in message_ids:
            try:
                message = await self._provider.read(message_id)
            except Exception as exc:
                log.warning(
                    "mail.received skipped; message fetch failed",
                    message_id=message_id,
                    error=str(exc),
                )
                continue
            payload: dict[str, Any] = {
                "message_id": message_id,
                "from": message.sender[:200],
                "subject": (message.subject or "(no subject)")[:200],
                "folder": _primary_folder(message.label_ids, reconciled_label=label),
                "has_attachments": bool(message.attachments),
                "provider": self._provider_name,
            }
            try:
                await emit_event(
                    self._bus,
                    tenant_id=self._tenant,
                    module="mail",
                    event_type="mail.received",
                    dedup_key=message_id,
                    payload=payload,
                    entity_ref=EntityRef(
                        ref_id=message_id,
                        module="mail",
                        kind="message",
                        title=payload["subject"],
                        summary=payload["from"],
                    ),
                )
            except Exception as exc:  # a spine hiccup must never cost the cache write already made
                log.warning("mail.received emit failed", message_id=message_id, error=str(exc))

    async def _emit_backlog(self, label: str, *, since: datetime) -> None:
        """Replay ``mail.received`` for the window a resumed sync missed (#796).

        Called only from a *resumed* full sync — the mailbox was synced before, the change
        cursor can no longer bridge the gap, and the plain full sync would otherwise restore
        every row while announcing none of them. Asks the provider what arrived after *since*
        (capped at ``resume_backlog_limit``) and hands those ids to the same
        :meth:`_emit_received` the delta path uses, so a replayed event is indistinguishable
        from a live one to every consumer.

        Best-effort by construction. The capability is optional
        (:meth:`MailProvider.messages_since` defaults to ``[]``), a provider error here is
        logged and dropped rather than failing a full sync whose *actual* job — restoring the
        cache — has already succeeded, and a truncated replay is logged so the shortfall is
        visible instead of assumed.
        """
        if self._bus is None or self._resume_backlog_limit <= 0:
            return
        try:
            message_ids = await self._provider.messages_since(
                since_ms=int(since.timestamp() * 1000), limit=self._resume_backlog_limit
            )
        except Exception as exc:
            log.warning("mail backlog replay unavailable", tenant=self._tenant, error=str(exc))
            return
        if not message_ids:
            return
        if len(message_ids) >= self._resume_backlog_limit:
            log.warning(
                "mail backlog replay truncated; older missed mail is not announced",
                tenant=self._tenant,
                limit=self._resume_backlog_limit,
            )
        log.info(
            "replaying mail backlog after a sync gap",
            tenant=self._tenant,
            count=len(message_ids),
            since=since.isoformat(),
        )
        await self._emit_received(label, set(message_ids))

    async def _emit_sync_failed(self, *, reason: str) -> None:
        """Emit ``mail.sync_failed``, rate-limited so a flapping account can't storm the bus.

        A cooldown, not a fire-once marker: the account may keep failing reconcile after
        reconcile (every mailbox page open triggers one), and each failure is real — the
        operator just doesn't need telling on every single poll.
        """
        if self._bus is None:
            return
        now = time.monotonic()
        if (
            self._last_sync_failed_at is not None
            and now - self._last_sync_failed_at < self._sync_failed_cooldown_s
        ):
            return
        self._last_sync_failed_at = now
        occurred_at = datetime.now(UTC)
        try:
            await emit_event(
                self._bus,
                tenant_id=self._tenant,
                module="mail",
                event_type="mail.sync_failed",
                dedup_key=f"{reason}:{occurred_at.isoformat()}",
                payload={"reason": reason, "provider": self._provider_name},
                occurred_at=occurred_at,
            )
        except Exception as exc:
            log.warning("mail.sync_failed emit failed", reason=reason, error=str(exc))

    # ── write-through ────────────────────────────────────────────────────────

    async def mark_thread_read(self, thread_id: str, *, unread: bool = False) -> None:
        """Flip a thread's cached ``unread`` flag at once (read/unread convergence, #623/#625).

        The cache half of an optimistic mark-read: the list reflects the new state before the
        provider round-trips. The provider write and its later history delta keep the two
        converged (a mark elsewhere flows back in through :meth:`reconcile`).

        Also drops the cached category tabs (#765): the thread just left (or joined) some
        tab's unread count, and which tab that is isn't knowable here without a fetch. The
        shell's existing post-mark invalidation therefore re-reads the page and the badge is
        already right — no full reload, and no stale count sitting there for a TTL.
        """
        await self._cache.set_thread_unread(
            tenant_id=self._tenant, thread_id=thread_id, unread=unread
        )
        await self._cache.invalidate_categories(tenant_id=self._tenant)

    # ── internals ────────────────────────────────────────────────────────────

    async def _full_sync(self, label: str) -> LandingBundle:
        """Fetch the folder's landing page + rail live, replace the cache, stamp the cursor.

        Also decides the one thing the no-firehose rule has to get right (#796): whether this
        sync is a **first** or a **resume**. ``synced_at`` answers it — absent means this
        mailbox has never been synced, so there is no new-vs-seen to report and the sync stays
        silent; present means we synced successfully at that instant and have since lost the
        thread (a cursor Gmail no longer retains, a wiped cache), so everything that arrived
        after it *is* news and is replayed by :meth:`_emit_backlog`. Read the stamp before
        :meth:`MailCache.set_cursor` overwrites it below.
        """
        resumed_from = await self._cache.get_last_synced_at(tenant_id=self._tenant)
        # Snapshot the cursor BEFORE fetching: a change during the fetch is then replayed by the
        # next reconcile rather than lost. Reconcile is idempotent, so replaying is harmless.
        snapshot = await self._provider.current_cursor()
        labels = await self._provider.list_labels(count_ids=self._count_ids(label))
        page = await self._provider.list_threads(
            label=label, query=None, cursor=None, limit=self._page_size
        )
        await self._cache.replace_labels(tenant_id=self._tenant, labels=labels)
        await self._cache.replace_landing(
            tenant_id=self._tenant,
            label=label,
            threads=page.threads,
            next_cursor=page.next_cursor,
        )
        await self._cache.set_cursor(tenant_id=self._tenant, cursor=snapshot)
        if resumed_from is not None:
            await self._emit_backlog(label, since=resumed_from)
        return LandingBundle(
            labels=labels,
            threads=list(page.threads[: self._landing_size]),
            next_cursor=page.next_cursor,
        )

    async def _apply_changes(self, label: str, thread_ids: set[str]) -> None:
        """Rebuild exactly the rows a delta touched — the "pull only the delta" core.

        For each changed thread: a fresh summary decides its fate. Gone (``None``) → drop it
        everywhere. Still in this folder → upsert (a new message re-sorts it to the top; a
        flag flip updates ``unread``). No longer in this folder → drop its row here (archived
        or moved out). A brand-new in-folder thread is simply inserted.
        """
        for thread_id in thread_ids:
            summary = await self._provider.get_thread_summary(thread_id)
            if summary is None:
                await self._cache.remove_thread(tenant_id=self._tenant, thread_id=thread_id)
            elif label in summary.label_ids:
                await self._cache.upsert_thread_row(
                    tenant_id=self._tenant, label=label, summary=summary
                )
            else:
                await self._cache.remove_thread_from_label(
                    tenant_id=self._tenant, label=label, thread_id=thread_id
                )
        await self._cache.prune_landing(tenant_id=self._tenant, label=label)

    async def _bundle_from_cache(self, label: str) -> LandingBundle:
        """Assemble the landing view purely from cache (no provider call)."""
        labels = await self._cache.get_labels(tenant_id=self._tenant)
        rows = await self._cache.get_landing(
            tenant_id=self._tenant, label=label, limit=self._landing_size
        )
        next_cursor = await self._cache.get_landing_cursor(tenant_id=self._tenant, label=label)
        return LandingBundle(labels=labels, threads=rows, next_cursor=next_cursor)

    def _count_ids(self, label: str) -> tuple[str, ...]:
        """The labels whose unread counts to fill: the nav-badge folder + the active one."""
        return (self._default_label, label)


def _primary_folder(label_ids: list[str], *, reconciled_label: str) -> str:
    """One representative folder for a ``mail.received`` payload (#663).

    A message often carries several labels at once; the event payload wants one value, not
    the full set. Prefers the label actually being reconciled (the view the operator is
    watching), then ``INBOX`` if the message has it (a near-universal folder name, not a
    Gmail-only convention), then whichever label came back first — provider-neutral
    throughout, no Gmail-specific label ordering here.
    """
    if reconciled_label in label_ids:
        return reconciled_label
    if "INBOX" in label_ids:
        return "INBOX"
    return label_ids[0] if label_ids else reconciled_label
