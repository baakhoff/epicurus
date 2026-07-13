"""Unit tests for the cache-first mailbox orchestrator (ADR-0096, #623).

Drives a real SQLite :class:`MailCache` through :class:`CachedMailbox` with a mocked
provider, so the landing/reconcile logic is tested end-to-end against actual persistence
(the provider is the only seam mocked). Asserts both the data *and* the provider call counts,
since the whole point of the cache is to *avoid* provider calls.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_mail.cache import CachedMailbox
from epicurus_mail.db import MailCache
from epicurus_mail.provider import (
    MailCursor,
    MailLabel,
    MailProvider,
    MailThreadSummary,
    ThreadChanges,
    ThreadPage,
)

TENANT = "local"


def _engine() -> AsyncEngine:
    return create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


def _summary(
    tid: str, *, unread: bool = False, sort_ts: int = 0, labels: tuple[str, ...] = ("INBOX",)
) -> MailThreadSummary:
    return MailThreadSummary(
        id=tid,
        subject=f"s-{tid}",
        sender="a@x.com",
        snippet="snip",
        date="",
        unread=unread,
        message_count=1,
        sort_ts=sort_ts,
        label_ids=list(labels),
    )


def _provider(
    *,
    threads: list[MailThreadSummary] | None = None,
    labels: list[MailLabel] | None = None,
    cursor: MailCursor | None = None,
) -> AsyncMock:
    provider = AsyncMock(spec=MailProvider)
    provider.current_cursor = AsyncMock(return_value=cursor or MailCursor(history_id=1000))
    default_labels = [MailLabel(id="INBOX", title="Inbox", unread=1)]
    provider.list_labels = AsyncMock(return_value=labels if labels is not None else default_labels)
    provider.list_threads = AsyncMock(
        return_value=ThreadPage(threads=threads or [_summary("t1", sort_ts=100)], next_cursor="NX")
    )
    return provider


async def _mailbox(provider: AsyncMock) -> tuple[CachedMailbox, MailCache]:
    cache = MailCache(_engine())
    await cache.init()
    return CachedMailbox(provider, cache, tenant_id=TENANT), cache  # type: ignore[arg-type]


# ── landing ──────────────────────────────────────────────────────────────────


async def test_landing_cold_does_a_full_sync() -> None:
    provider = _provider(threads=[_summary("t1", sort_ts=100)], cursor=MailCursor(history_id=555))
    mailbox, cache = await _mailbox(provider)
    bundle = await mailbox.landing("INBOX")
    assert [t.id for t in bundle.threads] == ["t1"]
    assert bundle.next_cursor == "NX"
    provider.list_threads.assert_awaited_once()  # cold → one live fetch
    # The cursor is snapshotted from current_cursor() BEFORE the list fetch.
    assert (await cache.get_cursor(tenant_id=TENANT)).history_id == 555


async def test_landing_warm_serves_from_cache_without_provider() -> None:
    provider = _provider(threads=[_summary("t1", sort_ts=100)])
    mailbox, _ = await _mailbox(provider)
    await mailbox.landing("INBOX")  # cold → populates
    provider.list_threads.reset_mock()
    provider.list_labels.reset_mock()
    bundle = await mailbox.landing("INBOX")  # warm → cache only
    assert [t.id for t in bundle.threads] == ["t1"]
    provider.list_threads.assert_not_awaited()
    provider.list_labels.assert_not_awaited()


# ── reconcile ────────────────────────────────────────────────────────────────


async def test_reconcile_empty_delta_advances_cursor_without_refetch() -> None:
    provider = _provider(threads=[_summary("t1", sort_ts=100)], cursor=MailCursor(history_id=100))
    mailbox, cache = await _mailbox(provider)
    await mailbox.landing("INBOX")  # seed
    provider.list_threads.reset_mock()
    provider.get_thread_summary = AsyncMock()
    provider.changed_threads_since = AsyncMock(
        return_value=ThreadChanges(changed_thread_ids=set(), next_cursor=MailCursor(history_id=200))
    )
    bundle = await mailbox.reconcile("INBOX")
    assert [t.id for t in bundle.threads] == ["t1"]  # unchanged, from cache
    provider.list_threads.assert_not_awaited()  # nothing changed → no page refetch
    provider.get_thread_summary.assert_not_awaited()  # nothing changed → no row refetch
    assert (await cache.get_cursor(tenant_id=TENANT)).history_id == 200  # cursor advanced


async def test_reconcile_updates_changed_thread_row() -> None:
    provider = _provider(threads=[_summary("t1", unread=True, sort_ts=100)])
    mailbox, _ = await _mailbox(provider)
    await mailbox.landing("INBOX")
    # t1 was read elsewhere: history reports it changed, and its fresh summary is now read.
    provider.changed_threads_since = AsyncMock(
        return_value=ThreadChanges(
            changed_thread_ids={"t1"}, next_cursor=MailCursor(history_id=200)
        )
    )
    provider.get_thread_summary = AsyncMock(return_value=_summary("t1", unread=False, sort_ts=100))
    bundle = await mailbox.reconcile("INBOX")
    assert bundle.threads[0].unread is False  # flag converged from the provider side


async def test_reconcile_inserts_new_in_label_thread_at_top() -> None:
    provider = _provider(threads=[_summary("t1", sort_ts=100)])
    mailbox, _ = await _mailbox(provider)
    await mailbox.landing("INBOX")
    provider.changed_threads_since = AsyncMock(
        return_value=ThreadChanges(
            changed_thread_ids={"t2"}, next_cursor=MailCursor(history_id=200)
        )
    )
    # A brand-new inbox thread, newer than t1.
    provider.get_thread_summary = AsyncMock(
        return_value=_summary("t2", sort_ts=300, labels=("INBOX",))
    )
    bundle = await mailbox.reconcile("INBOX")
    assert [t.id for t in bundle.threads] == ["t2", "t1"]  # new one on top (larger sort_ts)


async def test_reconcile_drops_thread_that_left_the_label() -> None:
    provider = _provider(threads=[_summary("t1", sort_ts=100)])
    mailbox, _ = await _mailbox(provider)
    await mailbox.landing("INBOX")
    provider.changed_threads_since = AsyncMock(
        return_value=ThreadChanges(
            changed_thread_ids={"t1"}, next_cursor=MailCursor(history_id=200)
        )
    )
    # t1 was archived: it still exists but no longer carries INBOX.
    provider.get_thread_summary = AsyncMock(return_value=_summary("t1", labels=("IMPORTANT",)))
    bundle = await mailbox.reconcile("INBOX")
    assert bundle.threads == []  # gone from the Inbox landing


async def test_reconcile_drops_deleted_thread() -> None:
    provider = _provider(threads=[_summary("t1", sort_ts=100)])
    mailbox, _ = await _mailbox(provider)
    await mailbox.landing("INBOX")
    provider.changed_threads_since = AsyncMock(
        return_value=ThreadChanges(
            changed_thread_ids={"t1"}, next_cursor=MailCursor(history_id=200)
        )
    )
    provider.get_thread_summary = AsyncMock(return_value=None)  # deleted at the provider
    bundle = await mailbox.reconcile("INBOX")
    assert bundle.threads == []


async def test_reconcile_cold_cursor_full_syncs() -> None:
    provider = _provider(threads=[_summary("t1", sort_ts=100)])
    mailbox, _ = await _mailbox(provider)
    # No landing seed → cursor is empty. reconcile must full-sync, not diff a null cursor.
    provider.changed_threads_since = AsyncMock()
    bundle = await mailbox.reconcile("INBOX")
    assert [t.id for t in bundle.threads] == ["t1"]
    provider.changed_threads_since.assert_not_awaited()  # cold → skip the diff
    provider.list_threads.assert_awaited_once()


async def test_reconcile_expired_cursor_full_resyncs() -> None:
    provider = _provider(threads=[_summary("t1", sort_ts=100)])
    mailbox, _ = await _mailbox(provider)
    await mailbox.landing("INBOX")
    provider.list_threads.reset_mock()
    # Gmail history expired → changed_threads_since returns None → full resync.
    provider.changed_threads_since = AsyncMock(return_value=None)
    provider.get_thread_summary = AsyncMock()
    bundle = await mailbox.reconcile("INBOX")
    assert [t.id for t in bundle.threads] == ["t1"]
    provider.list_threads.assert_awaited_once()  # fell back to a full page fetch
    provider.get_thread_summary.assert_not_awaited()  # resync, not per-thread patch


# ── write-through ─────────────────────────────────────────────────────────────


async def test_mark_thread_read_flips_cache_optimistically() -> None:
    provider = _provider(threads=[_summary("t1", unread=True, sort_ts=100)])
    mailbox, cache = await _mailbox(provider)
    await mailbox.landing("INBOX")
    await mailbox.mark_thread_read("t1")
    rows = await cache.get_landing(tenant_id=TENANT, label="INBOX", limit=25)
    assert rows[0].unread is False  # reflected before any provider round-trip
