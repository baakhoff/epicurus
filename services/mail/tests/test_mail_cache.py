"""Unit tests for the cache-first mailbox orchestrator (ADR-0096, #623).

Drives a real SQLite :class:`MailCache` through :class:`CachedMailbox` with a mocked
provider, so the landing/reconcile logic is tested end-to-end against actual persistence
(the provider is the only seam mocked). Asserts both the data *and* the provider call counts,
since the whole point of the cache is to *avoid* provider calls.

The ``*_emits_*``/``*_sync_failed*`` tests below (#663) additionally drive a
:class:`_RecordingBus` fake to pin the event-spine emission behavior: genuinely-new messages
only, nothing at all on a **first-ever** sync, and a rate-limited ``mail.sync_failed`` on
provider failure. Since #796 the full-sync half of that rule is split in two — a sync that
*resumes* a mailbox synced before replays the window it missed, while a first-ever one stays
silent — so the tests state which of the two they mean.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core import EventEnvelope
from epicurus_mail.cache import (
    DEFAULT_RESUME_BACKLOG_LIMIT,
    CachedMailbox,
    _primary_folder,
)
from epicurus_mail.db import MailCache
from epicurus_mail.provider import (
    MailAttachment,
    MailCategory,
    MailCategoryPreview,
    MailCursor,
    MailLabel,
    MailMessage,
    MailProvider,
    MailThreadSummary,
    ThreadChanges,
    ThreadPage,
)

TENANT = "local"


class _RecordingBus:
    """Captures publishes instead of talking to NATS (mirrors echo's test fake)."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, object], str | None]] = []

    async def publish(self, subject: str, data: object, tenant_id: str | None = None) -> None:
        assert isinstance(data, dict)
        self.published.append((subject, data, tenant_id))

    def envelopes(self) -> list[EventEnvelope]:
        return [EventEnvelope.model_validate(data) for _, data, _ in self.published]

    def envelopes_of_type(self, event_type: str) -> list[EventEnvelope]:
        return [e for e in self.envelopes() if e.type == event_type]


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
    # The backlog-replay capability (#796) is opt-in on the seam; state the empty default here so
    # a test that doesn't care about it can't be surprised by an auto-stubbed mock.
    provider.messages_since = AsyncMock(return_value=[])
    return provider


async def _mailbox(provider: AsyncMock) -> tuple[CachedMailbox, MailCache]:
    cache = MailCache(_engine())
    await cache.init()
    return CachedMailbox(provider, cache, tenant_id=TENANT), cache  # type: ignore[arg-type]


async def _mailbox_with_bus(
    provider: AsyncMock,
    bus: _RecordingBus,
    *,
    sync_failed_cooldown_s: float = 900.0,
    resume_backlog_limit: int = DEFAULT_RESUME_BACKLOG_LIMIT,
) -> CachedMailbox:
    cache = MailCache(_engine())
    await cache.init()
    return CachedMailbox(
        provider,
        cache,
        tenant_id=TENANT,
        bus=bus,  # type: ignore[arg-type]
        sync_failed_cooldown_s=sync_failed_cooldown_s,
        resume_backlog_limit=resume_backlog_limit,
    )


def _message(
    mid: str,
    *,
    thread_id: str = "t1",
    sender: str = "a@x.com",
    subject: str = "hi",
    labels: tuple[str, ...] = ("INBOX",),
    attachments: bool = False,
) -> MailMessage:
    return MailMessage(
        id=mid,
        thread_id=thread_id,
        subject=subject,
        sender=sender,
        to=["me@x.com"],
        date="",
        snippet="snip",
        body="the full body — must never reach an event payload",
        label_ids=list(labels),
        attachments=[MailAttachment(id="a1", filename="f.pdf")] if attachments else [],
    )


# ── _primary_folder ──────────────────────────────────────────────────────────


def test_primary_folder_prefers_the_reconciled_label() -> None:
    assert _primary_folder(["INBOX", "IMPORTANT"], reconciled_label="IMPORTANT") == "IMPORTANT"


def test_primary_folder_falls_back_to_inbox() -> None:
    assert _primary_folder(["IMPORTANT", "INBOX"], reconciled_label="SENT") == "INBOX"


def test_primary_folder_falls_back_to_the_first_label() -> None:
    assert _primary_folder(["CUSTOM", "IMPORTANT"], reconciled_label="SENT") == "CUSTOM"


def test_primary_folder_falls_back_to_the_reconciled_label_when_empty() -> None:
    assert _primary_folder([], reconciled_label="INBOX") == "INBOX"


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


# ── mail.received / mail.sync_failed emission (#663) ────────────────────────


async def test_reconcile_emits_mail_received_for_a_new_message() -> None:
    provider = _provider(threads=[_summary("t1", sort_ts=100)])
    bus = _RecordingBus()
    mailbox = await _mailbox_with_bus(provider, bus)
    await mailbox.landing("INBOX")
    provider.changed_threads_since = AsyncMock(
        return_value=ThreadChanges(
            changed_thread_ids={"t1"},
            new_message_ids={"m1"},
            next_cursor=MailCursor(history_id=200),
        )
    )
    provider.get_thread_summary = AsyncMock(return_value=_summary("t1", sort_ts=300))
    provider.read = AsyncMock(
        return_value=_message("m1", sender="alice@x.com", subject="Hello", attachments=True)
    )
    await mailbox.reconcile("INBOX")

    [envelope] = bus.envelopes_of_type("mail.received")
    assert envelope.module == "mail"
    assert envelope.dedup_key == "m1"  # the provider message id, per #663
    assert envelope.payload == {
        "message_id": "m1",
        "from": "alice@x.com",
        "subject": "Hello",
        "folder": "INBOX",
        "has_attachments": True,
        "provider": "gmail",
    }
    assert envelope.entity_ref is not None
    assert envelope.entity_ref.ref_id == "m1"
    assert envelope.entity_ref.kind == "message"


async def test_mail_received_payload_never_carries_the_body() -> None:
    # The envelope's own 4096-byte cap would eventually catch a body, but the payload
    # should never even attempt to carry one — pointers only (module_events.py's contract).
    provider = _provider(threads=[_summary("t1", sort_ts=100)])
    bus = _RecordingBus()
    mailbox = await _mailbox_with_bus(provider, bus)
    await mailbox.landing("INBOX")
    provider.changed_threads_since = AsyncMock(
        return_value=ThreadChanges(new_message_ids={"m1"}, next_cursor=MailCursor(history_id=200))
    )
    provider.read = AsyncMock(return_value=_message("m1"))
    await mailbox.reconcile("INBOX")

    [envelope] = bus.envelopes_of_type("mail.received")
    assert "body" not in envelope.payload
    assert "full body" not in str(envelope.payload)


async def test_reconcile_does_not_emit_for_a_flag_only_change() -> None:
    # t1 was read elsewhere: it's in changed_thread_ids (the row still needs a refresh) but
    # not in new_message_ids — a flag flip is not new mail.
    provider = _provider(threads=[_summary("t1", unread=True, sort_ts=100)])
    bus = _RecordingBus()
    mailbox = await _mailbox_with_bus(provider, bus)
    await mailbox.landing("INBOX")
    provider.changed_threads_since = AsyncMock(
        return_value=ThreadChanges(
            changed_thread_ids={"t1"}, next_cursor=MailCursor(history_id=200)
        )
    )
    provider.get_thread_summary = AsyncMock(return_value=_summary("t1", unread=False, sort_ts=100))
    await mailbox.reconcile("INBOX")
    assert bus.envelopes_of_type("mail.received") == []


async def test_reconcile_empty_delta_emits_nothing() -> None:
    provider = _provider(threads=[_summary("t1", sort_ts=100)])
    bus = _RecordingBus()
    mailbox = await _mailbox_with_bus(provider, bus)
    await mailbox.landing("INBOX")
    provider.changed_threads_since = AsyncMock(
        return_value=ThreadChanges(next_cursor=MailCursor(history_id=200))
    )
    await mailbox.reconcile("INBOX")
    assert bus.published == []


async def test_landing_first_ever_full_sync_never_emits_mail_received() -> None:
    # The no-firehose rule (#663): a mailbox that has never been synced has no prior state to
    # diff against, so a full sync's rows must never be reported as "new mail". Unchanged by
    # #796 — the backlog replay applies only where a *previous* successful sync is on record.
    provider = _provider(threads=[_summary("t1", sort_ts=100), _summary("t2", sort_ts=200)])
    provider.messages_since = AsyncMock(return_value=["m1", "m2"])  # would be a firehose
    bus = _RecordingBus()
    mailbox = await _mailbox_with_bus(provider, bus)
    await mailbox.landing("INBOX")
    assert bus.published == []
    provider.messages_since.assert_not_awaited()  # never even asked


async def test_reconcile_first_ever_full_sync_never_emits_mail_received() -> None:
    provider = _provider(threads=[_summary("t1", sort_ts=100)])
    provider.messages_since = AsyncMock(return_value=["m1"])
    bus = _RecordingBus()
    mailbox = await _mailbox_with_bus(provider, bus)
    # No landing seed → no sync ever recorded → reconcile falls back to a full sync, same
    # no-firehose rule as the first-ever landing case above.
    await mailbox.reconcile("INBOX")
    assert bus.published == []
    provider.messages_since.assert_not_awaited()


async def test_reconcile_expired_cursor_resync_replays_the_missed_window() -> None:
    """A *resumed* full sync announces the gap it just papered over (#796).

    This mailbox has been synced before, so an expired cursor means "we lost the thread since a
    known instant", not "we have never looked". Before the background poller existed this path
    stayed silent, which is how a restart could swallow every message that arrived while the
    service was down.
    """
    provider = _provider(threads=[_summary("t1", sort_ts=100)])
    bus = _RecordingBus()
    mailbox = await _mailbox_with_bus(provider, bus)
    await mailbox.landing("INBOX")  # a first, successful sync — the stamp the replay resumes from
    provider.changed_threads_since = AsyncMock(return_value=None)  # history expired
    provider.get_thread_summary = AsyncMock()
    provider.messages_since = AsyncMock(return_value=["m1", "m2"])
    provider.read = AsyncMock(side_effect=_message)

    await mailbox.reconcile("INBOX")

    assert {e.dedup_key for e in bus.envelopes_of_type("mail.received")} == {"m1", "m2"}
    # Bounded by the configured limit, and resumed from an instant, never from "the beginning".
    assert provider.messages_since.await_args.kwargs["limit"] == DEFAULT_RESUME_BACKLOG_LIMIT
    assert provider.messages_since.await_args.kwargs["since_ms"] > 0


async def test_the_replay_window_starts_at_the_last_successful_sync() -> None:
    """Not "now", and not the epoch: the stamp of the sync that last succeeded."""
    provider = _provider(threads=[_summary("t1", sort_ts=100)])
    bus = _RecordingBus()
    mailbox = await _mailbox_with_bus(provider, bus)
    before = datetime.now(UTC)
    await mailbox.landing("INBOX")
    after = datetime.now(UTC)
    provider.changed_threads_since = AsyncMock(return_value=None)
    provider.get_thread_summary = AsyncMock()
    provider.messages_since = AsyncMock(return_value=["m1"])
    provider.read = AsyncMock(side_effect=_message)

    await mailbox.reconcile("INBOX")

    since_ms = provider.messages_since.await_args.kwargs["since_ms"]
    # A second of slack each way: the stamp is written mid-`landing`, not at either boundary.
    assert int(before.timestamp() * 1000) - 1000 <= since_ms <= int(after.timestamp() * 1000) + 1000


async def test_a_truncated_backlog_still_replays_what_it_can() -> None:
    """The cap bounds the notification burst, it never turns the replay off."""
    provider = _provider(threads=[_summary("t1", sort_ts=100)])
    bus = _RecordingBus()
    mailbox = await _mailbox_with_bus(provider, bus, resume_backlog_limit=2)
    await mailbox.landing("INBOX")
    provider.changed_threads_since = AsyncMock(return_value=None)
    provider.get_thread_summary = AsyncMock()
    provider.messages_since = AsyncMock(return_value=["m1", "m2"])
    provider.read = AsyncMock(side_effect=_message)

    await mailbox.reconcile("INBOX")

    assert provider.messages_since.await_args.kwargs["limit"] == 2
    assert len(bus.envelopes_of_type("mail.received")) == 2


async def test_a_zero_backlog_limit_restores_the_old_silent_resync() -> None:
    """An operator who wants no replay at all gets no provider call for one, either."""
    provider = _provider(threads=[_summary("t1", sort_ts=100)])
    bus = _RecordingBus()
    mailbox = await _mailbox_with_bus(provider, bus, resume_backlog_limit=0)
    await mailbox.landing("INBOX")
    provider.changed_threads_since = AsyncMock(return_value=None)
    provider.get_thread_summary = AsyncMock()
    provider.messages_since = AsyncMock(return_value=["m1"])

    await mailbox.reconcile("INBOX")

    assert bus.envelopes_of_type("mail.received") == []
    provider.messages_since.assert_not_awaited()


async def test_a_provider_without_the_backlog_capability_syncs_silently() -> None:
    """``messages_since`` is a capability gate (ADR-0030): no replay, no crash, no blocked sync."""
    provider = _provider(threads=[_summary("t1", sort_ts=100)])
    bus = _RecordingBus()
    mailbox = await _mailbox_with_bus(provider, bus)
    await mailbox.landing("INBOX")
    provider.changed_threads_since = AsyncMock(return_value=None)
    provider.get_thread_summary = AsyncMock()
    provider.messages_since = AsyncMock(return_value=[])  # the seam's inherited default

    bundle = await mailbox.reconcile("INBOX")

    assert [t.id for t in bundle.threads] == ["t1"]  # the sync itself still landed
    assert bus.envelopes_of_type("mail.received") == []


async def test_a_failing_backlog_lookup_never_fails_the_full_sync() -> None:
    """Restoring the mailbox is the full sync's job; announcing the gap is best-effort."""
    provider = _provider(threads=[_summary("t1", sort_ts=100)])
    bus = _RecordingBus()
    mailbox = await _mailbox_with_bus(provider, bus)
    await mailbox.landing("INBOX")
    provider.changed_threads_since = AsyncMock(return_value=None)
    provider.get_thread_summary = AsyncMock()
    provider.messages_since = AsyncMock(side_effect=httpx.HTTPError("backlog search failed"))

    bundle = await mailbox.reconcile("INBOX")

    assert [t.id for t in bundle.threads] == ["t1"]
    assert bus.envelopes_of_type("mail.received") == []


async def test_reconcile_expired_cursor_resync_emits_sync_failed() -> None:
    provider = _provider(threads=[_summary("t1", sort_ts=100)])
    bus = _RecordingBus()
    mailbox = await _mailbox_with_bus(provider, bus)
    await mailbox.landing("INBOX")
    provider.changed_threads_since = AsyncMock(return_value=None)
    provider.get_thread_summary = AsyncMock()
    await mailbox.reconcile("INBOX")

    [envelope] = bus.envelopes_of_type("mail.sync_failed")
    assert envelope.payload == {"reason": "cursor_expired", "provider": "gmail"}


async def test_reconcile_provider_error_emits_sync_failed_and_reraises() -> None:
    provider = _provider(threads=[_summary("t1", sort_ts=100)])
    bus = _RecordingBus()
    mailbox = await _mailbox_with_bus(provider, bus)
    await mailbox.landing("INBOX")
    request = httpx.Request("GET", "http://gmail/history")
    provider.changed_threads_since = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "401", request=request, response=httpx.Response(401, request=request)
        )
    )
    with pytest.raises(httpx.HTTPStatusError):
        await mailbox.reconcile("INBOX")

    [envelope] = bus.envelopes_of_type("mail.sync_failed")
    assert envelope.payload == {"reason": "provider_error", "provider": "gmail"}


async def test_sync_failed_is_rate_limited() -> None:
    # A flapping account must not storm the bus: the same instance failing reconcile twice in
    # quick succession files only the first mail.sync_failed.
    provider = _provider(threads=[_summary("t1", sort_ts=100)])
    bus = _RecordingBus()
    mailbox = await _mailbox_with_bus(provider, bus, sync_failed_cooldown_s=900.0)
    await mailbox.landing("INBOX")
    provider.changed_threads_since = AsyncMock(return_value=None)
    provider.get_thread_summary = AsyncMock()

    await mailbox.reconcile("INBOX")
    await mailbox.reconcile("INBOX")

    assert len(bus.envelopes_of_type("mail.sync_failed")) == 1


async def test_sync_failed_fires_again_after_the_cooldown_elapses() -> None:
    provider = _provider(threads=[_summary("t1", sort_ts=100)])
    bus = _RecordingBus()
    # A cooldown of 0 means "always allowed" — proves the gate is time-based, not a one-shot.
    mailbox = await _mailbox_with_bus(provider, bus, sync_failed_cooldown_s=0.0)
    await mailbox.landing("INBOX")
    provider.changed_threads_since = AsyncMock(return_value=None)
    provider.get_thread_summary = AsyncMock()

    await mailbox.reconcile("INBOX")
    await mailbox.reconcile("INBOX")

    assert len(bus.envelopes_of_type("mail.sync_failed")) == 2


async def test_a_failed_message_fetch_is_skipped_not_fatal() -> None:
    # m1 vanished between history detection and the read (e.g. deleted); m2 must still emit.
    provider = _provider(threads=[_summary("t1", sort_ts=100)])
    bus = _RecordingBus()
    mailbox = await _mailbox_with_bus(provider, bus)
    await mailbox.landing("INBOX")
    provider.changed_threads_since = AsyncMock(
        return_value=ThreadChanges(
            new_message_ids={"m1", "m2"}, next_cursor=MailCursor(history_id=200)
        )
    )

    async def _read(message_id: str) -> MailMessage:
        if message_id == "m1":
            raise httpx.HTTPStatusError(
                "404",
                request=httpx.Request("GET", "http://gmail/m1"),
                response=httpx.Response(404),
            )
        return _message(message_id)

    provider.read = AsyncMock(side_effect=_read)
    await mailbox.reconcile("INBOX")

    received = bus.envelopes_of_type("mail.received")
    assert [e.dedup_key for e in received] == ["m2"]


async def test_no_bus_skips_emission_without_error() -> None:
    # A caller that only wants cache reads (most existing tests in this file) needs no NATS
    # connection — bus=None must not raise anywhere in the emission paths.
    provider = _provider(threads=[_summary("t1", sort_ts=100)])
    mailbox, _ = await _mailbox(provider)  # no bus
    await mailbox.landing("INBOX")
    provider.changed_threads_since = AsyncMock(
        return_value=ThreadChanges(new_message_ids={"m1"}, next_cursor=MailCursor(history_id=200))
    )
    provider.read = AsyncMock(return_value=_message("m1"))
    await mailbox.reconcile("INBOX")  # must not raise


# ── write-through ─────────────────────────────────────────────────────────────


async def test_mark_thread_read_flips_cache_optimistically() -> None:
    provider = _provider(threads=[_summary("t1", unread=True, sort_ts=100)])
    mailbox, cache = await _mailbox(provider)
    await mailbox.landing("INBOX")
    await mailbox.mark_thread_read("t1")
    rows = await cache.get_landing(tenant_id=TENANT, label="INBOX", limit=25)
    assert rows[0].unread is False  # reflected before any provider round-trip


# ── category tabs (#765) ─────────────────────────────────────────────────────


def _category(cid: str, title: str, *, unread: int = 1) -> MailCategory:
    return MailCategory(
        id=cid,
        title=title,
        unread=unread,
        preview=MailCategoryPreview(sender=f"{cid}@x.com", subject=f"newest-{cid}"),
    )


async def test_categories_are_assembled_once_then_served_from_cache() -> None:
    """The whole point: the provider fan-out must not run per render."""
    provider = _provider()
    provider.list_categories = AsyncMock(return_value=[_category("primary", "Primary")])
    mailbox, _ = await _mailbox(provider)

    first = await mailbox.categories("INBOX")
    second = await mailbox.categories("INBOX")

    assert [t.id for t in first] == [t.id for t in second] == ["primary"]
    provider.list_categories.assert_awaited_once_with(label="INBOX")


async def test_an_uncategorized_mailbox_is_probed_once_not_every_render() -> None:
    """An empty answer is cached too, else the negative case is the *most* expensive one."""
    provider = _provider()
    provider.list_categories = AsyncMock(return_value=[])
    mailbox, _ = await _mailbox(provider)

    assert await mailbox.categories("INBOX") == []
    assert await mailbox.categories("INBOX") == []
    provider.list_categories.assert_awaited_once()


async def test_an_expired_tab_set_is_reassembled() -> None:
    provider = _provider()
    provider.list_categories = AsyncMock(return_value=[_category("primary", "Primary")])
    cache = MailCache(_engine())
    await cache.init()
    mailbox = CachedMailbox(provider, cache, tenant_id=TENANT, category_ttl_s=-1)  # type: ignore[arg-type]

    await mailbox.categories("INBOX")
    await mailbox.categories("INBOX")

    assert provider.list_categories.await_count == 2


async def test_mark_thread_read_drops_the_tabs_so_the_badge_converges() -> None:
    """The acceptance path: the shell's existing invalidation re-reads and the count is right."""
    provider = _provider()
    provider.list_categories = AsyncMock(
        side_effect=[
            [_category("primary", "Primary", unread=3)],
            [_category("primary", "Primary", unread=2)],
        ]
    )
    mailbox, _ = await _mailbox(provider)

    assert (await mailbox.categories("INBOX"))[0].unread == 3
    await mailbox.mark_thread_read("t1")
    assert (await mailbox.categories("INBOX"))[0].unread == 2
    assert provider.list_categories.await_count == 2


async def test_mark_thread_read_still_flips_the_cached_row() -> None:
    """Invalidating the tabs must not cost the existing read/unread write-through (#625)."""
    provider = _provider(threads=[_summary("t1", unread=True, sort_ts=100)])
    provider.list_categories = AsyncMock(return_value=[])
    mailbox, cache = await _mailbox(provider)

    await mailbox.landing("INBOX")
    await mailbox.mark_thread_read("t1")

    rows = await cache.get_landing(tenant_id=TENANT, label="INBOX", limit=25)
    assert rows[0].unread is False


async def test_a_provider_failure_costs_the_tabs_not_the_page() -> None:
    """Tabs are an enhancement over the list — a page with mail beats a page with nothing."""
    provider = _provider()
    provider.list_categories = AsyncMock(side_effect=httpx.ConnectError("gmail down"))
    mailbox, _ = await _mailbox(provider)

    assert await mailbox.categories("INBOX") == []


async def test_a_failed_assembly_is_not_cached_as_no_categories() -> None:
    """A transient failure must not poison the negative cache for a whole TTL."""
    provider = _provider()
    provider.list_categories = AsyncMock(
        side_effect=[httpx.ConnectError("down"), [_category("primary", "Primary")]]
    )
    mailbox, _ = await _mailbox(provider)

    assert await mailbox.categories("INBOX") == []
    assert [t.id for t in await mailbox.categories("INBOX")] == ["primary"]
