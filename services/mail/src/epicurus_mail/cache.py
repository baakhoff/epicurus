"""Cache-first mailbox orchestration (ADR-0096, #623).

:class:`CachedMailbox` sits between the mailbox page reads and the provider, backed by the
tenant-scoped :class:`~epicurus_mail.db.MailCache`. It gives the landing view two speeds:

- :meth:`landing` — the **instant** path. Serves the cached rows + rail with no provider
  call (a cold cache falls through to a one-time full sync). This is what makes the *second*
  open of Mail render in ~a second instead of fanning out ~28 Gmail calls.
- :meth:`reconcile` — the **background** path. Asks the provider (via the neutral change
  cursor) what changed since the last sync and patches only those rows, so new/changed
  messages and flag flips appear without a manual refresh and without a full refetch.

Search and deeper (``cursor``) pages stay live — the cache only accelerates the default
landing view, which is the dogfood pain ("Mail takes far too long to open"). The
orchestrator is provider-neutral: it drives everything through the :class:`MailProvider`
seam, so an IMAP backend reuses it unchanged.

:meth:`reconcile` is also where ``mail.received`` and ``mail.sync_failed`` are emitted
(#663) — the one place a genuinely-new message or a broken sync is already known, rather
than duplicating that knowledge at every provider implementation. A cold/full sync never
emits ``mail.received`` (the no-firehose rule): it has no delta to report new-vs-seen
against, so treating a first load as "N new messages" would be noise, not news.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel, Field

from epicurus_core import EntityRef, EventBus, emit_event, get_logger
from epicurus_mail.db import MailCache
from epicurus_mail.provider import MailLabel, MailProvider, MailThreadSummary

log = get_logger("epicurus_mail.cache")


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

    # ── read paths ───────────────────────────────────────────────────────────

    async def landing(self, label: str) -> LandingBundle:
        """The instant landing view: cached rows + rail, or a one-time full sync when cold."""
        if await self._cache.has_landing(tenant_id=self._tenant, label=label):
            return await self._bundle_from_cache(label)
        return await self._full_sync(label)

    async def reconcile(self, label: str) -> LandingBundle:
        """Pull the delta since the last sync into the cache, then return the fresh landing.

        Cheap when idle: one ``changed_threads_since`` call that returns an empty delta just
        advances the cursor and re-serves the cache. When threads changed, only those rows are
        rebuilt (a single ``get_thread_summary`` each) and the rail's unread counts refreshed.
        A cold or expired cursor falls back to a full sync — which never emits ``mail.received``
        (#663's no-firehose rule): a full sync has no prior state to diff against, so every row
        would look "new" without actually being news.

        A provider failure (``httpx.HTTPError`` — an auth failure surfaces this way, via
        ``PlatformClient.get_oauth_token``) emits ``mail.sync_failed`` and re-raises, so the
        existing HTTP-level error mapping (403 scope hints, 429 throttling) is unchanged.
        """
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
        """
        await self._cache.set_thread_unread(
            tenant_id=self._tenant, thread_id=thread_id, unread=unread
        )

    # ── internals ────────────────────────────────────────────────────────────

    async def _full_sync(self, label: str) -> LandingBundle:
        """Fetch the folder's landing page + rail live, replace the cache, stamp the cursor."""
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
