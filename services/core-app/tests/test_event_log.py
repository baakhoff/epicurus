"""Tests for the module event spine's durable intake — store, intake, feed, retention.

The store's dedup and the intake's tenancy check are the two rules the rest of the spine
trusts, so both are tested for what they *reject*. The feed's history→live handover has a
subtle ordering property (an event landing mid-replay must not be lost) that is easy to
break and invisible in normal use, so it gets a test that forces the race.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core import EntityRef, Event, EventEnvelope
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


def _msg(envelope: EventEnvelope, *, subject: str | None = None) -> Event:
    """The envelope as it arrives off the wire, on its tenant-scoped subject."""
    return Event(
        subject=subject or f"{envelope.tenant_id}.events.{envelope.type}",
        data=envelope.model_dump_json().encode(),
    )


async def _fresh_store() -> EventLogStore:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    store = EventLogStore(engine)
    await store.init()
    return store


class _FakeBus:
    """Captures the intake's subscription instead of talking to NATS."""

    def __init__(self) -> None:
        self.subscribed: list[str] = []
        self.unsubscribed = 0

    async def subscribe_any_tenant(
        self, subject: str, handler: object, *, queue: str = ""
    ) -> object:
        self.subscribed.append(subject)
        outer = self

        class _Sub:
            async def unsubscribe(self) -> None:
                outer.unsubscribed += 1

        return _Sub()


async def _fresh_intake() -> tuple[EventLogStore, EventIntake, _FakeBus]:
    store = await _fresh_store()
    bus = _FakeBus()
    intake = EventIntake(store, bus)  # type: ignore[arg-type]  # structural: only subscribe_any_tenant
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


async def test_start_subscribes_across_tenants_and_is_idempotent() -> None:
    _store, intake, bus = await _fresh_intake()
    await intake.start()
    await intake.start()
    # One subscription, cross-tenant: a per-tenant list would silently miss a tenant added
    # at runtime.
    assert bus.subscribed == ["events.>"]
    await intake.stop()
    assert bus.unsubscribed == 1


async def test_handle_records_a_wire_event() -> None:
    store, intake, _bus = await _fresh_intake()
    await intake._handle(_msg(_envelope(dedup_key="k1")))
    rows = await store.recent(tenant=TENANT)
    assert [r.dedup_key for r in rows] == ["k1"]


async def test_handle_drops_malformed_json() -> None:
    store, intake, _bus = await _fresh_intake()
    await intake._handle(Event(subject="local.events.echo.pinged", data=b"not json"))
    assert await store.count() == 0  # logged and dropped; intake stays alive


async def test_handle_drops_a_payload_that_breaks_the_contract() -> None:
    # An emitter on an older library could put a credential or a mail body on the wire.
    # The contract is enforced on the way *in*, not merely requested at the source, so the
    # envelope's own validators reject it here and nothing is filed.
    store, intake, _bus = await _fresh_intake()
    raw = (
        '{"schema_version":1,"tenant_id":"local","module":"echo","type":"echo.pinged",'
        '"occurred_at":"2026-07-17T12:00:00Z","dedup_key":"k1","payload":{"api_key":"sk-1"}}'
    )
    await intake._handle(Event(subject="local.events.echo.pinged", data=raw.encode()))
    assert await store.count() == 0


async def test_handle_drops_a_tenant_mismatch() -> None:
    # The subject and the envelope are two independent tenant claims. A module publishing
    # one tenant's subject with another's envelope is buggy or hostile; either way the
    # event must not be filed under a guess.
    store, intake, _bus = await _fresh_intake()
    envelope = _envelope(tenant=OTHER_TENANT, dedup_key="k1")
    await intake._handle(_msg(envelope, subject="local.events.echo.pinged"))
    assert await store.count() == 0


async def test_listeners_fire_for_new_events_only() -> None:
    # The seam the automations matcher plugs into. A duplicate is not a change, so a
    # consumer must not see it twice.
    _store, intake, _bus = await _fresh_intake()
    seen: list[LoggedEvent] = []

    async def _listener(entry: LoggedEvent) -> None:
        seen.append(entry)

    intake.on_event(_listener)
    await intake._handle(_msg(_envelope(dedup_key="same")))
    await intake._handle(_msg(_envelope(dedup_key="same")))
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
    await intake._handle(_msg(_envelope(dedup_key="k1")))
    # The event is still recorded, and the second listener still ran.
    assert await store.count() == 1
    assert calls == ["bad", "good"]


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
    await intake._handle(_msg(_envelope(dedup_key="live")))
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

    store.recent = _slow_recent  # type: ignore[method-assign,assignment]

    agen = intake.stream(tenant=TENANT)
    pull = asyncio.create_task(agen.__anext__())
    await asyncio.sleep(0.05)  # the generator is now blocked inside the history query
    await intake._handle(_msg(_envelope(dedup_key="mid-replay")))
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
    await intake._handle(_msg(_envelope(tenant=OTHER_TENANT, dedup_key="theirs")))
    await intake._handle(_msg(_envelope(tenant=TENANT, dedup_key="mine")))
    entry = await asyncio.wait_for(pull, timeout=5)
    assert entry.dedup_key == "mine"  # the other tenant's event never surfaced
    await agen.aclose()


async def test_stream_filters_live_events_by_module() -> None:
    _store, intake, _bus = await _fresh_intake()
    agen = intake.stream(tenant=TENANT, module="mail")
    pull = asyncio.create_task(agen.__anext__())
    await asyncio.sleep(0.05)
    await intake._handle(_msg(_envelope(module="echo", dedup_key="e")))
    await intake._handle(_msg(_envelope(module="mail", event_type="mail.received", dedup_key="m")))
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

    store.prune = _boom  # type: ignore[method-assign,assignment]
    task = asyncio.create_task(retention.run_periodic())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls > 1  # it kept ticking after the failure
