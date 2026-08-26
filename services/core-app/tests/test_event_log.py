"""Tests for the module event spine's durable intake — store, intake, feed, retention.

The store's dedup and the intake's tenancy check are the two rules the rest of the spine
trusts, so both are tested for what they *reject*. The feed's history→live handover has a
subtle ordering property (an event landing mid-replay must not be lost) that is easy to
break and invisible in normal use, so it gets a test that forces the race.

Since the spine became at-least-once, one more rule joins them and it is the most important
of the three: **a message's disposition follows the durable write.** Acked once the row is
committed, naked when the store refused it, terminated when it can never be stored. Those
are asserted directly here — the fake message records what it was told, so the *ordering*
(row first, ack second) is a test rather than a comment. What only a real broker can prove —
that an unacked message actually comes back — lives in the integration suite.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from nats.aio.msg import Msg
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core import (
    EVENTS_DURABLE,
    EVENTS_STREAM,
    EVENTS_STREAM_SUBJECT,
    EntityRef,
    EventEnvelope,
)
from epicurus_core_app import event_log
from epicurus_core_app.event_log import (
    EventIntake,
    EventLogStore,
    EventRetention,
    LoggedEvent,
)

TENANT = "local"
OTHER_TENANT = "other"


# ── helpers ──────────────────────────────────────────────────────────────────


def _envelope(
    *,
    tenant: str = TENANT,
    module: str = "echo",
    event_type: str = "echo.pinged",
    dedup_key: str = "k1",
    payload: dict[str, object] | None = None,
    entity_ref: EntityRef | None = None,
    occurred_at: datetime | None = None,
) -> EventEnvelope:
    return EventEnvelope(
        tenant_id=tenant,
        module=module,
        type=event_type,
        occurred_at=occurred_at or datetime.now(UTC),
        dedup_key=dedup_key,
        payload=payload or {},
        entity_ref=entity_ref,
    )


class _FakeMsg:
    """A JetStream delivery that records its disposition instead of talking to a broker.

    ``dispositions`` is what makes the ack-after-commit rule testable: the store appends to
    the *same* list, so an assertion can state the ordering rather than trust it.
    """

    def __init__(self, subject: str, data: bytes, *, log: list[str] | None = None) -> None:
        self.subject = subject
        self.data = data
        self.dispositions = log if log is not None else []
        self.ack_error: Exception | None = None

    async def ack(self) -> None:
        self.dispositions.append("ack")
        if self.ack_error is not None:
            raise self.ack_error

    async def nak(self, delay: float | None = None) -> None:
        self.dispositions.append(f"nak:{delay}")

    async def term(self) -> None:
        self.dispositions.append("term")


def _msg(
    envelope: EventEnvelope, *, subject: str | None = None, log: list[str] | None = None
) -> _FakeMsg:
    """The envelope as it arrives off the wire, on its tenant-scoped subject."""
    return _FakeMsg(
        subject or f"{envelope.tenant_id}.events.{envelope.type}",
        envelope.model_dump_json().encode(),
        log=log,
    )


async def _consume(intake: EventIntake, msg: _FakeMsg) -> _FakeMsg:
    """Feed one delivery through the intake, returning the message for its dispositions."""
    await intake._consume(cast("Msg", msg))
    return msg


async def _fresh_store() -> EventLogStore:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    store = EventLogStore(engine)
    await store.init()
    return store


class _FakeSub:
    """A pull subscription that hands over queued batches, then idles like a real one."""

    def __init__(self) -> None:
        self.batches: list[list[_FakeMsg]] = []
        self.errors: list[Exception] = []
        self.unsubscribed = 0
        self.fetches = 0

    async def fetch(self, batch: int = 1, timeout: float | None = 5) -> list[Msg]:
        self.fetches += 1
        if self.errors:
            raise self.errors.pop(0)
        if self.batches:
            return [cast("Msg", m) for m in self.batches.pop(0)]
        # Nothing pending: park for the fetch window and time out, exactly as JetStream
        # does. Returning immediately would turn the intake's loop into a spin.
        await asyncio.sleep(timeout or 0)
        raise TimeoutError("nats: timeout")

    async def unsubscribe(self) -> None:
        self.unsubscribed += 1


class _FakeBus:
    """Captures the intake's provisioning and binding instead of talking to NATS."""

    def __init__(self) -> None:
        self.streams: list[tuple[str, list[str]]] = []
        self.subscribed: list[str] = []
        self.durables: list[str] = []
        self.bind_errors: list[Exception] = []
        self.sub = _FakeSub()

    async def ensure_stream(
        self, name: str, subjects: list[str], *, max_age_s: float, max_bytes: int
    ) -> None:
        if self.bind_errors:
            raise self.bind_errors.pop(0)
        self.streams.append((name, list(subjects)))

    async def pull_subscribe_any_tenant(
        self, subject: str, *, durable: str, stream: str, ack_wait_s: float
    ) -> _FakeSub:
        self.subscribed.append(subject)
        self.durables.append(durable)
        return self.sub

    @property
    def unsubscribed(self) -> int:
        return self.sub.unsubscribed


async def _fresh_intake() -> tuple[EventLogStore, EventIntake, _FakeBus]:
    store = await _fresh_store()
    bus = _FakeBus()
    # Structural: the intake only ever calls ensure_stream / pull_subscribe_any_tenant.
    intake = EventIntake(store, cast("Any", bus))
    return store, intake, bus


# ── the store ────────────────────────────────────────────────────────────────


async def test_append_records_the_envelope() -> None:
    store = await _fresh_store()
    ref = EntityRef(ref_id="e1", module="echo", kind="ping", title="hi")
    stored = await store.append(_envelope(payload={"n": 1}, entity_ref=ref))
    assert stored is not None
    assert stored.module == "echo"
    assert stored.type == "echo.pinged"
    assert stored.payload == {"n": 1}
    assert stored.entity_ref == ref
    assert stored.received_at is not None


async def test_duplicate_dedup_key_is_stored_once() -> None:
    # The acceptance criterion: a re-delivered change collapses to one row.
    store = await _fresh_store()
    first = await store.append(_envelope(dedup_key="same"))
    second = await store.append(_envelope(dedup_key="same"))
    assert first is not None
    assert second is None  # the duplicate reports a no-op rather than raising
    assert await store.count() == 1


async def test_dedup_is_scoped_per_module() -> None:
    # (tenant, module, dedup_key) — two modules may legitimately pick the same key for
    # unrelated changes, and neither should shadow the other.
    store = await _fresh_store()
    assert await store.append(_envelope(module="echo", dedup_key="same")) is not None
    assert (
        await store.append(_envelope(module="mail", event_type="mail.received", dedup_key="same"))
        is not None
    )
    assert await store.count() == 2


async def test_dedup_is_scoped_per_tenant() -> None:
    # Constraint #1: one tenant's key must never suppress another tenant's event.
    store = await _fresh_store()
    assert await store.append(_envelope(tenant=TENANT, dedup_key="same")) is not None
    assert await store.append(_envelope(tenant=OTHER_TENANT, dedup_key="same")) is not None
    assert await store.count(tenant=TENANT) == 1
    assert await store.count(tenant=OTHER_TENANT) == 1


async def test_first_write_wins_on_a_duplicate() -> None:
    # A later delivery of an already-recorded change carries no newer truth, so it must
    # not overwrite — the stored payload stays the one that got there first.
    store = await _fresh_store()
    await store.append(_envelope(dedup_key="same", payload={"v": "first"}))
    await store.append(_envelope(dedup_key="same", payload={"v": "second"}))
    rows = await store.recent(tenant=TENANT)
    assert [r.payload for r in rows] == [{"v": "first"}]


async def test_recent_is_newest_first_and_capped() -> None:
    store = await _fresh_store()
    for i in range(5):
        await store.append(_envelope(dedup_key=f"k{i}"))
    rows = await store.recent(tenant=TENANT, limit=3)
    assert [r.dedup_key for r in rows] == ["k4", "k3", "k2"]


async def test_recent_is_tenant_scoped() -> None:
    store = await _fresh_store()
    await store.append(_envelope(tenant=TENANT, dedup_key="mine"))
    await store.append(_envelope(tenant=OTHER_TENANT, dedup_key="theirs"))
    rows = await store.recent(tenant=TENANT)
    assert [r.dedup_key for r in rows] == ["mine"]


async def test_recent_filters_by_module_and_type() -> None:
    store = await _fresh_store()
    await store.append(_envelope(module="echo", event_type="echo.pinged", dedup_key="a"))
    await store.append(_envelope(module="mail", event_type="mail.received", dedup_key="b"))
    await store.append(_envelope(module="mail", event_type="mail.sent", dedup_key="c"))
    assert [r.dedup_key for r in await store.recent(tenant=TENANT, module="mail")] == ["c", "b"]
    assert [r.dedup_key for r in await store.recent(tenant=TENANT, event_type="mail.received")] == [
        "b"
    ]


async def test_recent_redacts_defensively() -> None:
    # The envelope refuses credential-shaped keys at emit, so this row could only exist if
    # it were written by an older/laxer path — which is exactly what a check at the
    # *surface* is for. Bypass the envelope to simulate that.
    store = await _fresh_store()
    stored = await store.append(_envelope(dedup_key="k", payload={"safe": 1}))
    assert stored is not None
    async with store._session() as session:
        from epicurus_core_app.event_log import _StoredEvent

        row = await session.get(_StoredEvent, stored.id)
        assert row is not None
        row.payload = {"safe": 1, "api_key": "sk-leak"}
        await session.commit()
    rows = await store.recent(tenant=TENANT)
    assert rows[0].payload == {"safe": 1}


async def test_prune_drops_only_old_rows() -> None:
    store = await _fresh_store()
    await store.append(_envelope(dedup_key="keep"))
    rows = await store.recent(tenant=TENANT)
    cutoff = rows[0].received_at + timedelta(seconds=1)
    assert await store.prune(older_than=cutoff) == 1
    assert await store.count() == 0


async def test_prune_keeps_rows_inside_the_window() -> None:
    store = await _fresh_store()
    await store.append(_envelope(dedup_key="keep"))
    assert await store.prune(older_than=datetime.now(UTC) - timedelta(days=1)) == 0
    assert await store.count() == 1


# ── the intake ───────────────────────────────────────────────────────────────


async def test_start_provisions_the_stream_and_binds_the_durable_consumer() -> None:
    _store, intake, bus = await _fresh_intake()
    await intake.start()
    try:
        # One stream over one cross-tenant subject: a per-tenant stream or consumer would
        # silently miss a tenant added at runtime (constraint #1).
        assert bus.streams == [(EVENTS_STREAM, [EVENTS_STREAM_SUBJECT])]
        assert bus.subscribed == ["events.>"]
        assert bus.durables == [EVENTS_DURABLE]
    finally:
        await intake.stop()
    assert bus.unsubscribed == 1


async def test_start_is_idempotent() -> None:
    # Called on every boot, and `start()` is re-entrant on a running intake. Provisioning
    # twice must not create a second stream or a second cursor — a second durable name is
    # how you silently replay the entire stream.
    _store, intake, bus = await _fresh_intake()
    await intake.start()
    await intake.start()
    try:
        assert len(bus.streams) == 1
        assert len(bus.subscribed) == 1
    finally:
        await intake.stop()


async def test_start_retries_a_cold_boot_race_with_nats(monkeypatch: pytest.MonkeyPatch) -> None:
    # compose starts the core on nats `service_started`, not on JetStream readiness, so a
    # cold boot can lose a race by a beat. Without the retry that race is *permanent*: the
    # spine records nothing until someone restarts the core.
    monkeypatch.setattr(event_log, "_BIND_RETRY_S", 0.01)
    _store, intake, bus = await _fresh_intake()
    bus.bind_errors = [RuntimeError("jetstream not ready")]
    await intake.start()
    try:
        assert len(bus.streams) == 1
    finally:
        await intake.stop()


async def test_start_raises_once_the_retries_are_spent(monkeypatch: pytest.MonkeyPatch) -> None:
    # Loud, not silent: the caller logs it and the core comes up without intake, which an
    # operator can see. A swallowed failure looks exactly like "no module emitted anything".
    monkeypatch.setattr(event_log, "_BIND_RETRY_S", 0.01)
    _store, intake, bus = await _fresh_intake()
    bus.bind_errors = [RuntimeError("nats down")] * 5
    with pytest.raises(RuntimeError, match="nats down"):
        await intake.start()


async def test_consume_records_a_wire_event_then_acks_it() -> None:
    """The ordering that *is* the at-least-once guarantee: row first, ack second.

    Both write into the same list, so this asserts the sequence rather than describing it.
    Reversed, a core that died between ack and commit would drop the event with the broker
    believing it delivered — the exact loss this transport removes.
    """
    store, intake, _bus = await _fresh_intake()
    order: list[str] = []
    real_append = store.append

    async def _tracking_append(envelope: EventEnvelope) -> LoggedEvent | None:
        result = await real_append(envelope)
        order.append("append")
        return result

    store.append = _tracking_append  # type: ignore[method-assign]

    msg = await _consume(intake, _msg(_envelope(dedup_key="k1"), log=order))
    assert msg.dispositions == ["append", "ack"]
    assert [r.dedup_key for r in await store.recent(tenant=TENANT)] == ["k1"]


async def test_consume_naks_when_the_store_refuses_the_event() -> None:
    # A database outage must cost latency, not history. Naked, so it comes back — the old
    # transport logged the exception and dropped the event on the floor.
    store, intake, _bus = await _fresh_intake()

    async def _boom(envelope: EventEnvelope) -> LoggedEvent | None:
        raise RuntimeError("db down")

    store.append = _boom  # type: ignore[method-assign]

    msg = await _consume(intake, _msg(_envelope(dedup_key="k1")))
    assert msg.dispositions == ["nak:5.0"]
    assert "ack" not in msg.dispositions


async def test_consume_terminates_malformed_json() -> None:
    # Terminate, not nak: unlimited redelivery plus an unparseable message is an infinite
    # loop, and no number of attempts will make `not json` parse.
    store, intake, _bus = await _fresh_intake()
    msg = await _consume(intake, _FakeMsg("local.events.echo.pinged", b"not json"))
    assert msg.dispositions == ["term"]
    assert await store.count() == 0  # logged and dropped; intake stays alive


async def test_consume_terminates_a_payload_that_breaks_the_contract() -> None:
    # An emitter on an older library could put a credential or a mail body on the wire.
    # The contract is enforced on the way *in*, not merely requested at the source, so the
    # envelope's own validators reject it here and nothing is filed.
    store, intake, _bus = await _fresh_intake()
    raw = (
        '{"schema_version":1,"tenant_id":"local","module":"echo","type":"echo.pinged",'
        '"occurred_at":"2026-07-17T12:00:00Z","dedup_key":"k1","payload":{"api_key":"sk-1"}}'
    )
    msg = await _consume(intake, _FakeMsg("local.events.echo.pinged", raw.encode()))
    assert msg.dispositions == ["term"]
    assert await store.count() == 0


async def test_consume_terminates_a_tenant_mismatch() -> None:
    # The subject and the envelope are two independent tenant claims. A module publishing
    # one tenant's subject with another's envelope is buggy or hostile; either way the
    # event must not be filed under a guess — and redelivering it would not change whose
    # it is.
    store, intake, _bus = await _fresh_intake()
    envelope = _envelope(tenant=OTHER_TENANT, dedup_key="k1")
    msg = await _consume(intake, _msg(envelope, subject="local.events.echo.pinged"))
    assert msg.dispositions == ["term"]
    assert await store.count() == 0


async def test_a_redelivered_event_is_recorded_once_and_acked() -> None:
    """Dedup is what makes at-least-once *safe*, and it is enforced, not advisory.

    The second delivery re-runs `append`, the unique constraint rejects the insert, and the
    consumer acks a no-op. One row, and the redelivery does not come back a third time.
    """
    store, intake, _bus = await _fresh_intake()
    first = await _consume(intake, _msg(_envelope(dedup_key="same")))
    second = await _consume(intake, _msg(_envelope(dedup_key="same")))
    assert first.dispositions == ["ack"]
    assert second.dispositions == ["ack"]
    assert await store.count() == 1


async def test_a_failed_ack_does_not_unwind_the_row() -> None:
    # A lost ack costs one redelivery, which dedup absorbs. Rolling the row back instead
    # would trade a harmless duplicate delivery for real data loss.
    store, intake, _bus = await _fresh_intake()
    msg = _msg(_envelope(dedup_key="k1"))
    msg.ack_error = RuntimeError("broker gone")
    await _consume(intake, msg)
    assert await store.count() == 1


async def test_listeners_fire_for_new_events_only() -> None:
    # The seam the automations matcher plugs into. A duplicate is not a change, so a
    # consumer must not see it twice — which is also why a redelivery is invisible to a
    # listener rather than a double-trigger.
    _store, intake, _bus = await _fresh_intake()
    seen: list[LoggedEvent] = []

    async def _listener(entry: LoggedEvent) -> None:
        seen.append(entry)

    intake.on_event(_listener)
    await _consume(intake, _msg(_envelope(dedup_key="same")))
    await _consume(intake, _msg(_envelope(dedup_key="same")))
    assert [e.dedup_key for e in seen] == ["same"]


async def test_a_raising_listener_does_not_break_intake() -> None:
    store, intake, _bus = await _fresh_intake()
    calls: list[str] = []

    async def _bad(_entry: LoggedEvent) -> None:
        calls.append("bad")
        raise RuntimeError("boom")

    async def _good(_entry: LoggedEvent) -> None:
        calls.append("good")

    intake.on_event(_bad)
    intake.on_event(_good)
    msg = await _consume(intake, _msg(_envelope(dedup_key="k1")))
    # The event is still recorded and acked, and the second listener still ran.
    assert await store.count() == 1
    assert calls == ["bad", "good"]
    assert msg.dispositions == ["ack"]


async def test_a_slow_listener_does_not_hold_the_ack() -> None:
    """Fan-out runs *after* the ack, so a wedged consumer cannot cause a redelivery storm.

    Holding the ack until every listener returned would let a slow one blow through
    ``ack_wait`` — and the redelivery it triggered would dedup to a no-op that skips the
    listeners anyway. All cost, no benefit.
    """
    _store, intake, _bus = await _fresh_intake()
    order: list[str] = []

    async def _slow(entry: LoggedEvent) -> None:
        await asyncio.sleep(0)  # a real listener yields; the ack must already be gone
        order.append("listener")

    intake.on_event(_slow)
    msg = await _consume(intake, _msg(_envelope(dedup_key="k1"), log=order))
    assert order == ["ack", "listener"]
    assert msg.dispositions is order


async def test_the_pull_loop_survives_a_failing_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    # A NATS hiccup must not end intake for the life of the process — the loop backs off
    # and keeps pulling.
    monkeypatch.setattr(event_log, "_FETCH_BACKOFF_S", 0.01)
    store, intake, bus = await _fresh_intake()
    bus.sub.errors = [RuntimeError("connection reset")]
    bus.sub.batches = [[_msg(_envelope(dedup_key="after-the-hiccup"))]]
    await intake.start()
    try:
        for _ in range(100):
            if await store.count():
                break
            await asyncio.sleep(0.05)
    finally:
        await intake.stop()
    assert bus.sub.fetches >= 2  # it pulled again after the failure
    assert [r.dedup_key for r in await store.recent(tenant=TENANT)] == ["after-the-hiccup"]


async def test_stop_is_safe_before_start_and_twice() -> None:
    # Shutdown runs on paths where startup failed, so stop() must never assume a consumer.
    _store, intake, _bus = await _fresh_intake()
    await intake.stop()
    await intake.start()
    await intake.stop()
    await intake.stop()


# ── the feed ─────────────────────────────────────────────────────────────────


async def test_stream_replays_history_oldest_first() -> None:
    store, intake, _bus = await _fresh_intake()
    for i in range(3):
        await store.append(_envelope(dedup_key=f"k{i}"))
    seen: list[str] = []
    agen = intake.stream(tenant=TENANT)
    try:
        async for entry in agen:
            seen.append(entry.dedup_key)
            if len(seen) == 3:
                break
    finally:
        await agen.aclose()
    # recent() is newest-first; a feed reads oldest-first.
    assert seen == ["k0", "k1", "k2"]


async def test_stream_yields_live_events_after_history() -> None:
    _store, intake, _bus = await _fresh_intake()
    agen = intake.stream(tenant=TENANT)
    pull = asyncio.create_task(agen.__anext__())
    await asyncio.sleep(0.05)  # let it register its queue and drain the empty history
    await _consume(intake, _msg(_envelope(dedup_key="live")))
    entry = await asyncio.wait_for(pull, timeout=5)
    assert entry.dedup_key == "live"
    await agen.aclose()


async def test_stream_does_not_lose_an_event_that_lands_during_replay() -> None:
    """The subscriber queue registers *before* the history query, so an event arriving
    mid-replay is queued rather than dropped.

    Invisible in normal use (the query is fast) and a correctness bug when it breaks, so
    force the race: hold the history query open, emit, then let it finish.
    """
    store, intake, _bus = await _fresh_intake()
    await store.append(_envelope(dedup_key="old"))
    gate = asyncio.Event()
    real_recent = store.recent

    async def _slow_recent(**kwargs: object) -> list[LoggedEvent]:
        await gate.wait()
        return await real_recent(**kwargs)  # type: ignore[arg-type]

    store.recent = _slow_recent  # type: ignore[method-assign]

    agen = intake.stream(tenant=TENANT)
    pull = asyncio.create_task(agen.__anext__())
    await asyncio.sleep(0.05)  # the generator is now blocked inside the history query
    await _consume(intake, _msg(_envelope(dedup_key="mid-replay")))
    gate.set()

    seen: list[str] = []
    try:
        seen.append((await asyncio.wait_for(pull, timeout=5)).dedup_key)
        seen.append((await asyncio.wait_for(agen.__anext__(), timeout=5)).dedup_key)
    finally:
        await agen.aclose()
    # The mid-replay event survived. (It may also appear in history — the caller
    # de-duplicates on id; a duplicated row is cosmetic, a missing one is not.)
    assert "mid-replay" in seen


async def test_stream_is_tenant_scoped() -> None:
    _store, intake, _bus = await _fresh_intake()
    agen = intake.stream(tenant=TENANT)
    pull = asyncio.create_task(agen.__anext__())
    await asyncio.sleep(0.05)
    await _consume(intake, _msg(_envelope(tenant=OTHER_TENANT, dedup_key="theirs")))
    await _consume(intake, _msg(_envelope(tenant=TENANT, dedup_key="mine")))
    entry = await asyncio.wait_for(pull, timeout=5)
    assert entry.dedup_key == "mine"  # the other tenant's event never surfaced
    await agen.aclose()


async def test_stream_filters_live_events_by_module() -> None:
    _store, intake, _bus = await _fresh_intake()
    agen = intake.stream(tenant=TENANT, module="mail")
    pull = asyncio.create_task(agen.__anext__())
    await asyncio.sleep(0.05)
    await _consume(intake, _msg(_envelope(module="echo", dedup_key="e")))
    await _consume(
        intake, _msg(_envelope(module="mail", event_type="mail.received", dedup_key="m"))
    )
    entry = await asyncio.wait_for(pull, timeout=5)
    assert entry.dedup_key == "m"
    await agen.aclose()


async def test_stream_unregisters_its_subscriber_on_close() -> None:
    # Otherwise every closed browser tab leaks a queue that intake keeps filling forever.
    _store, intake, _bus = await _fresh_intake()
    agen = intake.stream(tenant=TENANT)
    pull = asyncio.create_task(agen.__anext__())
    await asyncio.sleep(0.05)  # the generator has registered and is polling its queue
    assert len(intake._subscribers) == 1
    # Cancel *and await*: the cancellation only reaches the generator's `finally` once the
    # task it is suspended in actually unwinds. Skipping the await races aclose() against
    # a still-running generator — which is what a closing browser tab does, too.
    pull.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await pull
    await agen.aclose()
    assert intake._subscribers == []


# ── retention ────────────────────────────────────────────────────────────────


async def test_retention_keeps_a_row_inside_the_window() -> None:
    store = await _fresh_store()
    await store.append(_envelope(dedup_key="fresh"))
    assert await EventRetention(store, retention_days=30).prune_once() == 0
    assert await store.count() == 1


async def test_retention_zero_days_keeps_everything() -> None:
    store = await _fresh_store()
    await store.append(_envelope(dedup_key="k"))
    assert await EventRetention(store, retention_days=0).prune_once() == 0
    assert await EventRetention(store, retention_days=-1).prune_once() == 0
    assert await store.count() == 1


async def test_retention_removes_rows_older_than_the_window() -> None:
    store = await _fresh_store()
    stored = await store.append(_envelope(dedup_key="old"))
    assert stored is not None
    # Backdate the row past a 1-day window.
    async with store._session() as session:
        from epicurus_core_app.event_log import _StoredEvent

        row = await session.get(_StoredEvent, stored.id)
        assert row is not None
        row.received_at = datetime.now(UTC) - timedelta(days=3)
        await session.commit()
    assert await EventRetention(store, retention_days=1).prune_once() == 1
    assert await store.count() == 0


async def test_retention_loop_survives_a_failing_prune() -> None:
    # A transient DB error must not kill the loop for the life of the process.
    store = await _fresh_store()
    retention = EventRetention(store, retention_days=1, interval_s=0)
    calls = 0

    async def _boom(**_kwargs: object) -> int:
        nonlocal calls
        calls += 1
        raise RuntimeError("db down")

    store.prune = _boom  # type: ignore[method-assign]
    task = asyncio.create_task(retention.run_periodic())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls > 1  # it kept ticking after the failure
