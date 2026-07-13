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
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from epicurus_mail.db import MailCache
from epicurus_mail.provider import MailLabel, MailProvider, MailThreadSummary


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
    ) -> None:
        self._provider = provider
        self._cache = cache
        self._tenant = tenant_id
        self._default_label = default_label
        self._page_size = page_size
        self._landing_size = landing_size

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
        A cold or expired cursor falls back to a full sync.
        """
        cursor = await self._cache.get_cursor(tenant_id=self._tenant)
        if cursor.is_empty():
            return await self._full_sync(label)
        changes = await self._provider.changed_threads_since(cursor)
        if changes is None:  # cursor too old to replay → full resync
            return await self._full_sync(label)
        if changes.changed_thread_ids:
            await self._apply_changes(label, changes.changed_thread_ids)
            labels = await self._provider.list_labels(count_ids=self._count_ids(label))
            await self._cache.replace_labels(tenant_id=self._tenant, labels=labels)
        await self._cache.set_cursor(tenant_id=self._tenant, cursor=changes.next_cursor)
        return await self._bundle_from_cache(label)

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
