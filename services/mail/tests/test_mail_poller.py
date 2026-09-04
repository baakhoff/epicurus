"""Unit tests for the background mailbox reconcile (#796).

The bug these exist to keep fixed: ``mail.received`` used to be emitted from one method that
only ran when a human opened the Mail page, so an automation on "new mail arrived" never fired
unattended. **Nothing in this file ever performs a page read** — that is the point of it.

Everything is driven through a real SQLite :class:`MailCache` with a mocked provider, so the
poller is exercised against actual persistence. The tests that touch the store from two tasks at
once (the poller's task and the test's own) use a **file-backed** database with default pooling,
never in-memory + ``StaticPool``: that fixture shares one DBAPI connection across every session,
and a reader's checkout-return ``ROLLBACK`` can land inside a concurrent writer's transaction and
silently erase it — a failure the product cannot have (production is Postgres) but the test
would happily manufacture.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from epicurus_core import EventEnvelope
from epicurus_mail.cache import CachedMailbox
from epicurus_mail.db import MailCache
from epicurus_mail.poller import (
    DEFAULT_POLL_INTERVAL_S,
    DEFAULT_POLL_LABEL,
    MAX_UNREACHABLE_BACKOFF,
    run_periodic,
    tick,
)
from epicurus_mail.provider import (
    MailAvailability,
    MailAvailabilityState,
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
    """Captures publishes instead of talking to NATS (mirrors the cache tests' fake)."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, object], str | None]] = []

    async def publish(self, subject: str, data: object, tenant_id: str | None = None) -> None:
        assert isinstance(data, dict)
        self.published.append((subject, data, tenant_id))

    def envelopes_of_type(self, event_type: str) -> list[EventEnvelope]:
        return [
            env
            for env in (EventEnvelope.model_validate(data) for _, data, _ in self.published)
            if env.type == event_type
        ]


class _RecordingLog:
    """A stand-in for the module's structlog logger, so "logs nothing noisy" is assertable.

    Asserted against directly rather than through ``structlog.testing.capture_logs``: structlog
    is configured process-globally by whichever test booted an app first, and a bound logger is
    cached on first use — both of which make a capture-based assertion silently unreliable here.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def _record(self, level: str) -> Callable[..., None]:
        def _log(event: str, **_kw: Any) -> None:
            self.calls.append((level, event))

        return _log

    def __getattr__(self, level: str) -> Callable[..., None]:
        return self._record(level)

    def events(self, level: str) -> list[str]:
        return [event for lvl, event in self.calls if lvl == level]


class _Deltas:
    """Hands out one scripted delta per call, then "nothing changed" forever.

    A list ``side_effect`` would raise ``StopIteration`` once exhausted, which the poll loop
    would dutifully swallow as a failed tick — hiding the very thing under test.

    Pass the bound :meth:`next_delta`, never the instance: ``AsyncMock`` only awaits a
    ``side_effect`` that ``iscoroutinefunction`` recognizes, and an object with an ``async
    __call__`` is not one — it would hand the caller an un-awaited coroutine instead.
    """

    def __init__(self, *deltas: ThreadChanges) -> None:
        self._queue = list(deltas)
        self.seen_cursors: list[int | None] = []

    async def next_delta(self, cursor: MailCursor) -> ThreadChanges:
        self.seen_cursors.append(cursor.history_id)
        if self._queue:
            return self._queue.pop(0)
        return ThreadChanges(next_cursor=cursor)


def _summary(tid: str) -> MailThreadSummary:
    return MailThreadSummary(
        id=tid, subject=f"s-{tid}", sender="a@x.com", snippet="snip", date="", label_ids=["INBOX"]
    )


def _message(mid: str) -> MailMessage:
    return MailMessage(
        id=mid,
        thread_id="t1",
        subject=f"subject-{mid}",
        sender="a@x.com",
        to=["me@x.com"],
        date="",
        snippet="snip",
        label_ids=["INBOX"],
    )


def _provider(*, available: bool = True, state: MailAvailabilityState | None = None) -> AsyncMock:
    """A provider stub with the whole reconcile surface stubbed to a quiet, valid default.

    *state* names the availability outright (#835); *available* is the older two-state
    shorthand, kept because most tests only care whether a tick reconciles.
    """
    provider = AsyncMock(spec=MailProvider)
    resolved: MailAvailabilityState = state or ("connected" if available else "not_connected")
    provider.availability = AsyncMock(
        return_value=MailAvailability(
            state=resolved,
            reason=None if resolved == "connected" else f"stubbed {resolved}",
        )
    )
    provider.current_cursor = AsyncMock(return_value=MailCursor(history_id=1000))
    provider.list_labels = AsyncMock(return_value=[MailLabel(id="INBOX", title="Inbox", unread=1)])
    provider.list_threads = AsyncMock(
        return_value=ThreadPage(threads=[_summary("t1")], next_cursor=None)
    )
    provider.get_thread_summary = AsyncMock(return_value=_summary("t1"))
    # "Nothing changed", *with* an advanced cursor — a bare ``ThreadChanges()`` carries an empty
    # one, which would blank the stored cursor and send every later tick back through full sync.
    provider.changed_threads_since = AsyncMock(
        return_value=ThreadChanges(next_cursor=MailCursor(history_id=1001))
    )
    provider.messages_since = AsyncMock(return_value=[])
    provider.read = AsyncMock(side_effect=lambda mid: _message(mid))
    return provider


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    """A **file-backed** SQLite engine with default pooling (see the module docstring).

    Disposed before the test's loop closes: each aiosqlite connection owns a worker thread, and
    an undisposed engine leaves them raising "Event loop is closed" at GC.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mail-cache.db'}")
    yield engine
    await engine.dispose()


async def _mailbox(
    engine: AsyncEngine,
    provider: AsyncMock,
    bus: _RecordingBus,
    *,
    resume_backlog_limit: int = 50,
) -> tuple[CachedMailbox, MailCache]:
    cache = MailCache(engine)
    await cache.init()
    mailbox = CachedMailbox(
        provider,
        cache,
        tenant_id=TENANT,
        bus=bus,  # type: ignore[arg-type]
        resume_backlog_limit=resume_backlog_limit,
    )
    return mailbox, cache


async def _wait_for(predicate: Callable[[], bool], *, timeout: float = 5.0) -> bool:
    """Poll *predicate* until true, or give up — the poller runs in its own task."""

    async def _poll() -> bool:
        while not predicate():
            await asyncio.sleep(0.01)
        return True

    try:
        return await asyncio.wait_for(_poll(), timeout=timeout)
    except TimeoutError:
        return predicate()


async def _run_ticks(coro_kwargs: dict[str, Any], *, until: Callable[[], bool]) -> None:
    """Run the poll loop until *until* holds (or the wait times out), then cancel it cleanly."""
    task = asyncio.create_task(run_periodic(**coro_kwargs))
    try:
        await _wait_for(until)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


# ── the assertion that would have caught the bug ─────────────────────────────


async def test_poller_emits_mail_received_with_no_page_read_ever(engine: AsyncEngine) -> None:
    """New mail arrives, nobody opens Mail, the event fires anyway (#796).

    The mailbox is warm (a stored cursor), so each tick takes the cheap delta path — exactly
    what the page's ``?reconcile=1`` used to be the only trigger for.
    """
    provider = _provider()
    deltas = _Deltas(
        ThreadChanges(
            changed_thread_ids={"t1"},
            new_message_ids={"m1"},
            next_cursor=MailCursor(history_id=101),
        )
    )
    provider.changed_threads_since = AsyncMock(side_effect=deltas.next_delta)
    bus = _RecordingBus()
    mailbox, cache = await _mailbox(engine, provider, bus)
    await cache.set_cursor(tenant_id=TENANT, cursor=MailCursor(history_id=100))

    await _run_ticks(
        {
            "mailbox": mailbox,
            "provider": provider,
            "tenant": TENANT,
            "poll_interval_s": 0.01,
        },
        until=lambda: bool(bus.envelopes_of_type("mail.received")),
    )

    received = bus.envelopes_of_type("mail.received")
    assert [e.payload["message_id"] for e in received] == ["m1"]
    assert received[0].tenant_id == TENANT
    # No page was read: no landing/full-sync fetch happened, only the incremental delta.
    provider.list_threads.assert_not_awaited()


async def test_poller_keeps_ticking_and_reports_each_new_arrival(engine: AsyncEngine) -> None:
    """Mail arriving on a *later* interval is announced too — the loop, not just a first pass."""
    provider = _provider()
    deltas = _Deltas(
        ThreadChanges(new_message_ids={"m1"}, next_cursor=MailCursor(history_id=101)),
        ThreadChanges(new_message_ids={"m2"}, next_cursor=MailCursor(history_id=102)),
    )
    provider.changed_threads_since = AsyncMock(side_effect=deltas.next_delta)
    bus = _RecordingBus()
    mailbox, cache = await _mailbox(engine, provider, bus)
    await cache.set_cursor(tenant_id=TENANT, cursor=MailCursor(history_id=100))

    await _run_ticks(
        {
            "mailbox": mailbox,
            "provider": provider,
            "tenant": TENANT,
            "poll_interval_s": 0.01,
        },
        until=lambda: len(bus.envelopes_of_type("mail.received")) >= 2,
    )

    assert {e.payload["message_id"] for e in bus.envelopes_of_type("mail.received")} == {"m1", "m2"}
    # Each tick resumed from the cursor the previous one persisted — a second pass really did
    # happen, and it did not re-ask from the same starting point.
    assert deltas.seen_cursors[:2] == [100, 101]


# ── no double-emit: the poller racing a page reconcile ───────────────────────


async def test_poller_and_a_concurrent_page_reconcile_emit_once_for_one_message(
    engine: AsyncEngine,
) -> None:
    """One arrival, two concurrent reconcilers, exactly one ``mail.received`` (#796).

    The delta call sleeps, so both callers are genuinely in flight together. Single-flight makes
    the loser re-read the cursor the winner advanced, which is why it finds nothing to announce —
    asserted on the *cursors the provider saw*, since that is the mechanism, not a coincidence.
    """
    provider = _provider()
    seen: list[int | None] = []

    async def _changed(cursor: MailCursor) -> ThreadChanges:
        seen.append(cursor.history_id)
        await asyncio.sleep(0.05)  # hold the window open for the other caller
        if (cursor.history_id or 0) < 2:
            return ThreadChanges(
                changed_thread_ids={"t1"},
                new_message_ids={"m1"},
                next_cursor=MailCursor(history_id=2),
            )
        return ThreadChanges(next_cursor=cursor)

    provider.changed_threads_since = AsyncMock(side_effect=_changed)
    bus = _RecordingBus()
    mailbox, cache = await _mailbox(engine, provider, bus)
    await cache.set_cursor(tenant_id=TENANT, cursor=MailCursor(history_id=1))

    await asyncio.gather(
        tick(mailbox=mailbox, provider=provider, label="INBOX"),  # the background poller
        mailbox.reconcile("INBOX"),  # the operator opening Mail at the same moment
    )

    assert [e.payload["message_id"] for e in bus.envelopes_of_type("mail.received")] == ["m1"]
    assert seen == [1, 2]  # serialized: the second call started from the advanced cursor
    assert (await cache.get_cursor(tenant_id=TENANT)).history_id == 2


async def test_a_cold_cache_is_full_synced_once_under_two_racing_callers(
    engine: AsyncEngine,
) -> None:
    """A poll tick and a first page open must not both run the ~28-call full-sync fan-out."""
    provider = _provider()
    bus = _RecordingBus()
    mailbox, _ = await _mailbox(engine, provider, bus)

    await asyncio.gather(mailbox.landing("INBOX"), mailbox.landing("INBOX"))

    provider.list_threads.assert_awaited_once()


# ── an idle deployment: nothing done, nothing said ───────────────────────────


async def test_tick_does_nothing_when_no_account_is_connected() -> None:
    provider = _provider(available=False)
    mailbox = AsyncMock(spec=CachedMailbox)

    availability = await tick(mailbox=mailbox, provider=provider, label="INBOX")

    assert availability.state == "not_connected"
    mailbox.reconcile.assert_not_awaited()
    provider.current_cursor.assert_not_awaited()
    provider.changed_threads_since.assert_not_awaited()


async def test_the_loop_logs_nothing_per_tick_while_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unconnected deployment must not narrate its own inactivity every interval."""
    provider = _provider(available=False)
    mailbox = AsyncMock(spec=CachedMailbox)
    recorder = _RecordingLog()
    monkeypatch.setattr("epicurus_mail.poller.log", recorder)

    await _run_ticks(
        {
            "mailbox": mailbox,
            "provider": provider,
            "tenant": TENANT,
            "poll_interval_s": 0.01,
        },
        until=lambda: provider.availability.await_count >= 3,
    )

    assert provider.availability.await_count >= 3  # it really did tick repeatedly
    assert recorder.events("info") == ["mail background reconcile started"]  # once, at startup
    assert recorder.events("warning") == []


async def test_a_zero_interval_disables_the_loop_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider()
    mailbox = AsyncMock(spec=CachedMailbox)
    recorder = _RecordingLog()
    monkeypatch.setattr("epicurus_mail.poller.log", recorder)

    # Returns rather than spins: no task is left running and the provider is never touched.
    await asyncio.wait_for(
        run_periodic(mailbox=mailbox, provider=provider, tenant=TENANT, poll_interval_s=0),
        timeout=1,
    )

    provider.availability.assert_not_awaited()
    mailbox.reconcile.assert_not_awaited()
    assert recorder.events("info") == ["mail background reconcile disabled"]


# ── failure handling ─────────────────────────────────────────────────────────


async def test_a_failing_tick_never_kills_the_loop_and_warns_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken account produces one warning and a recovery line, not a warning per interval."""
    provider = _provider()
    mailbox = AsyncMock(spec=CachedMailbox)
    failures = 3

    async def _reconcile(label: str) -> None:
        if mailbox.reconcile.await_count <= failures:
            raise RuntimeError("token expired")

    mailbox.reconcile = AsyncMock(side_effect=_reconcile)
    recorder = _RecordingLog()
    monkeypatch.setattr("epicurus_mail.poller.log", recorder)

    await _run_ticks(
        {
            "mailbox": mailbox,
            "provider": provider,
            "tenant": TENANT,
            "poll_interval_s": 0.01,
        },
        until=lambda: "mail background reconcile recovered" in recorder.events("info"),
    )

    assert recorder.events("warning") == ["mail background reconcile failed"]
    assert recorder.events("info") == [
        "mail background reconcile started",
        "mail background reconcile recovered",
    ]
    assert mailbox.reconcile.await_count > failures  # the loop survived every failure


# ── restart with a cold cursor (#796's fourth point) ─────────────────────────


async def test_a_restart_whose_cursor_expired_replays_the_missed_mail(
    engine: AsyncEngine,
) -> None:
    """The service was down; the change cursor no longer replays; the backlog is still announced.

    This is the case the no-firehose rule used to swallow whole: a full sync restores every row
    and reports none of them, so "notify me when mail arrives" silently skipped the entire
    outage. A mailbox that has been synced before now replays what arrived since that instant.
    """
    provider = _provider()
    provider.changed_threads_since = AsyncMock(return_value=None)  # history expired
    provider.messages_since = AsyncMock(return_value=["m1", "m2"])
    bus = _RecordingBus()
    mailbox, cache = await _mailbox(engine, provider, bus)
    # A previous run of the service synced successfully an hour ago, then stopped.
    await cache.set_cursor(tenant_id=TENANT, cursor=MailCursor(history_id=100))

    await _run_ticks(
        {
            "mailbox": mailbox,
            "provider": provider,
            "tenant": TENANT,
            "poll_interval_s": 0.01,
        },
        until=lambda: len(bus.envelopes_of_type("mail.received")) >= 2,
    )

    assert {e.payload["message_id"] for e in bus.envelopes_of_type("mail.received")} == {"m1", "m2"}
    # The gap itself is still reported, so the operator can see *why* a replay happened.
    assert bus.envelopes_of_type("mail.sync_failed")[0].payload["reason"] == "cursor_expired"
    # The replay window starts at the last successful sync, not at "now".
    since_ms = provider.messages_since.await_args.kwargs["since_ms"]
    assert since_ms <= int(datetime.now(UTC).timestamp() * 1000)
    assert since_ms > int((datetime.now(UTC) - timedelta(minutes=5)).timestamp() * 1000)


async def test_a_first_ever_sync_still_announces_nothing(engine: AsyncEngine) -> None:
    """The no-firehose rule is untouched where it was right: an unsynced mailbox is not news."""
    provider = _provider()
    provider.messages_since = AsyncMock(return_value=["m1", "m2", "m3"])
    bus = _RecordingBus()
    mailbox, _ = await _mailbox(engine, provider, bus)

    await _run_ticks(
        {
            "mailbox": mailbox,
            "provider": provider,
            "tenant": TENANT,
            "poll_interval_s": 0.01,
        },
        until=lambda: provider.list_threads.await_count >= 1,
    )

    assert bus.envelopes_of_type("mail.received") == []
    provider.messages_since.assert_not_awaited()  # never even asked


# ── wiring ───────────────────────────────────────────────────────────────────


async def test_the_service_lifespan_starts_and_stops_the_poller() -> None:
    """The loop is only useful if the app actually runs it — and stops it on shutdown."""
    from fastapi.testclient import TestClient

    started: list[dict[str, Any]] = []
    cancelled: list[bool] = []

    async def _fake_run_periodic(**kwargs: Any) -> None:
        started.append(kwargs)
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    provider = _provider()
    provider.health_check = AsyncMock(return_value=True)
    provider.list_categories = AsyncMock(return_value=[])
    with (
        patch.dict(os.environ, {"MAIL_POLL_INTERVAL_S": "42"}),
        patch("epicurus_mail.app.GmailProvider", return_value=provider),
        patch("epicurus_mail.app.EventBus.from_settings", return_value=AsyncMock()),
        patch("epicurus_mail.app.run_periodic", _fake_run_periodic),
    ):
        from epicurus_mail.app import create_app

        app = create_app(engine=create_async_engine("sqlite+aiosqlite://"))
        with TestClient(app):
            pass

    assert len(started) == 1
    assert started[0]["poll_interval_s"] == 42.0  # from MAIL_POLL_INTERVAL_S
    assert started[0]["tenant"] == TENANT
    assert cancelled == [True]  # cancelled on shutdown, not left orphaned


def test_the_defaults_are_the_documented_ones() -> None:
    """Pinned because they are a product decision, not an implementation detail (#796)."""
    from epicurus_mail.settings import MailSettings

    assert DEFAULT_POLL_INTERVAL_S == 300.0  # five minutes
    assert DEFAULT_POLL_LABEL == "INBOX"
    # On by default: the whole point is that the event fires unattended. Read off the field
    # rather than an instance, so an operator's own MAIL_POLL_INTERVAL_S can't mask a change.
    assert MailSettings.model_fields["mail_poll_interval_s"].default == DEFAULT_POLL_INTERVAL_S


# ── an unreachable provider is not an idle one (#835) ────────────────────────
# Before the three-state signal the loop could not tell "nobody connected Google" from "the
# core didn't answer", so it took the idle path for both: no reconcile, no log, no change of
# pace. A mailbox could stop syncing for as long as the core was down and leave nothing behind
# saying why. These tests pin the distinction at every level of the loop.


async def test_tick_reports_unreachable_and_does_not_reconcile() -> None:
    provider = _provider(state="unreachable")
    mailbox = AsyncMock(spec=CachedMailbox)

    availability = await tick(mailbox=mailbox, provider=provider, label="INBOX")

    assert availability.state == "unreachable"
    # It must not reconcile — but the caller is told *why* it didn't, which is the whole
    # difference from the not-connected tick above.
    mailbox.reconcile.assert_not_awaited()


async def test_tick_reconciles_only_on_connected() -> None:
    mailbox = AsyncMock(spec=CachedMailbox)
    cases: tuple[tuple[MailAvailabilityState, bool], ...] = (
        ("connected", True),
        ("not_connected", False),
        ("unreachable", False),
    )
    for state, reconciles in cases:
        mailbox.reconcile.reset_mock()
        availability = await tick(mailbox=mailbox, provider=_provider(state=state), label="INBOX")
        assert availability.state == state
        assert (mailbox.reconcile.await_count == 1) is reconciles


async def test_an_unreachable_provider_warns_once_and_names_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The log line an operator greps for when mail quietly stopped syncing (#835)."""
    provider = _provider(state="unreachable")
    mailbox = AsyncMock(spec=CachedMailbox)
    recorder = _RecordingLog()
    monkeypatch.setattr("epicurus_mail.poller.log", recorder)

    await _run_ticks(
        {
            "mailbox": mailbox,
            "provider": provider,
            "tenant": TENANT,
            "poll_interval_s": 0.01,
        },
        until=lambda: provider.availability.await_count >= 3,
    )

    # Once, not per interval — and never the silence the not-connected state gets.
    assert recorder.events("warning") == ["mail provider unreachable; background reconcile paused"]
    assert recorder.events("debug").count("mail provider still unreachable") >= 1
    mailbox.reconcile.assert_not_awaited()


async def test_a_not_connected_provider_still_says_nothing_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The counterpart guarantee: the honest absence must stay silent (#796's first property)."""
    provider = _provider(state="not_connected")
    mailbox = AsyncMock(spec=CachedMailbox)
    recorder = _RecordingLog()
    monkeypatch.setattr("epicurus_mail.poller.log", recorder)

    await _run_ticks(
        {
            "mailbox": mailbox,
            "provider": provider,
            "tenant": TENANT,
            "poll_interval_s": 0.01,
        },
        until=lambda: provider.availability.await_count >= 3,
    )

    assert recorder.events("warning") == []
    assert recorder.events("info") == ["mail background reconcile started"]


async def test_the_loop_logs_a_recovery_when_the_core_comes_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider()
    states = [
        MailAvailability(state="unreachable", reason="core down"),
        MailAvailability(state="unreachable", reason="core down"),
        MailAvailability(state="connected"),
    ]

    async def _availability() -> MailAvailability:
        return states.pop(0) if states else MailAvailability(state="connected")

    provider.availability = AsyncMock(side_effect=_availability)
    mailbox = AsyncMock(spec=CachedMailbox)
    recorder = _RecordingLog()
    monkeypatch.setattr("epicurus_mail.poller.log", recorder)

    await _run_ticks(
        {
            "mailbox": mailbox,
            "provider": provider,
            "tenant": TENANT,
            "poll_interval_s": 0.01,
        },
        until=lambda: "mail background reconcile recovered" in recorder.events("info"),
    )

    assert recorder.events("warning") == ["mail provider unreachable; background reconcile paused"]
    assert recorder.events("info") == [
        "mail background reconcile started",
        "mail background reconcile recovered",
    ]
    mailbox.reconcile.assert_awaited()  # and it resumed reconciling


async def test_consecutive_unreachable_ticks_back_off_and_reset_on_an_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A downed core is probed on a widening schedule, not at full rate forever (#835).

    Asserted on the *sleeps the loop asks for*, since the pacing is the behaviour — a wall-clock
    assertion would be a flake generator. The reset matters as much as the widening: without it
    a mailbox would stay stale long after the core returned.
    """
    provider = _provider()
    states = [
        MailAvailability(state="unreachable", reason="core down"),
        MailAvailability(state="unreachable", reason="core down"),
        MailAvailability(state="unreachable", reason="core down"),
        MailAvailability(state="unreachable", reason="core down"),
        MailAvailability(state="connected"),
    ]
    slept: list[float] = []

    async def _availability() -> MailAvailability:
        return states.pop(0) if states else MailAvailability(state="connected")

    real_sleep = asyncio.sleep

    async def _sleep(delay: float) -> None:
        # `epicurus_mail.poller.asyncio` *is* the global module, so this patch is process-wide
        # and also catches the test harness's own poll sleeps; only the loop's own delays are
        # >= 1s, which is why the interval below is 10s and the assertion filters.
        slept.append(delay)
        await real_sleep(0)  # yield without actually waiting

    provider.availability = AsyncMock(side_effect=_availability)
    monkeypatch.setattr("epicurus_mail.poller.asyncio.sleep", _sleep)
    mailbox = AsyncMock(spec=CachedMailbox)
    monkeypatch.setattr("epicurus_mail.poller.log", _RecordingLog())

    await _run_ticks(
        {
            "mailbox": mailbox,
            "provider": provider,
            "tenant": TENANT,
            "poll_interval_s": 10.0,
        },
        until=lambda: len([d for d in slept if d >= 1.0]) >= 5,
    )

    # Doubling per consecutive unreachable tick, capped, then straight back to the interval.
    assert [d for d in slept if d >= 1.0][:5] == [10.0, 20.0, 40.0, 40.0, 10.0]
    assert MAX_UNREACHABLE_BACKOFF == 4.0


async def test_a_raised_tick_and_an_unreachable_tick_log_differently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two failures, two messages: the reconcile itself blew up vs. it never got to run."""
    provider = _provider()
    mailbox = AsyncMock(spec=CachedMailbox)
    mailbox.reconcile = AsyncMock(side_effect=RuntimeError("gmail 500"))
    recorder = _RecordingLog()
    monkeypatch.setattr("epicurus_mail.poller.log", recorder)

    await _run_ticks(
        {
            "mailbox": mailbox,
            "provider": provider,
            "tenant": TENANT,
            "poll_interval_s": 0.01,
        },
        until=lambda: bool(recorder.events("warning")),
    )

    assert recorder.events("warning") == ["mail background reconcile failed"]
    assert "mail provider unreachable; background reconcile paused" not in recorder.events(
        "warning"
    )
