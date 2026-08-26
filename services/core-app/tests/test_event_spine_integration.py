"""Integration test: the module event spine over real NATS. Requires Docker.

The unit tests drive intake with a fake bus, which will happily accept any subject string
— including one a real broker would reject or route differently. Two things only a real
server can prove:

* **The routing.** That ``*.events.>`` actually matches a tenant-scoped envelope subject,
  across *every* tenant, and nothing else. The failure mode is silent: a wildcard that does
  not match produces no error, no log, and no event — an intake that appears healthy and
  records nothing.
* **The durability.** That the stream holds what a stopped consumer has not taken, that a
  restarted consumer resumes rather than replaying or skipping, and that an unacked message
  actually comes back. A fake can be *told* those things; only a broker can be asked.

The store is a *file-backed* SQLite, not the in-memory + ``StaticPool`` one the unit tests
use. Here the intake writes from its pull task while the test polls ``count()`` from its own
task, and ``StaticPool`` hands both sessions the *same* DBAPI connection — the pool's
reset-``ROLLBACK`` on each poll checkout can then land inside ``append``'s ``BEGIN…COMMIT``
and silently erase the insert (the append still returns a row, and the next insert re-uses
its id). A file database gives every session its own connection, which is also what
production Postgres does; the unit tests keep the in-memory store because they never touch
it from two tasks at once.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from nats.aio.msg import Msg
from nats.js.errors import NotFoundError
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

from epicurus_core import EVENTS_STREAM, EventBus, emit_event
from epicurus_core_app.event_log import EventIntake, EventLogStore

pytestmark = pytest.mark.integration

# Short enough that a redelivery lands inside a test, long enough that a healthy handler on
# a loaded CI box is never mistaken for a dead one.
ACK_WAIT_S = 2.0


@pytest.fixture(scope="module")
def nats_url() -> Iterator[str]:
    container = DockerContainer("nats:2.10").with_command("-js").with_exposed_ports(4222)
    with container:
        wait_for_logs(container, "Server is ready")
        yield f"nats://{container.get_container_host_ip()}:{container.get_exposed_port(4222)}"


@pytest.fixture
async def bus(nats_url: str) -> AsyncIterator[EventBus]:
    """A connected bus on a server holding no spine stream.

    The stream and its durable cursor are *server-side* state that outlives a test. Left in
    place, the second test to run would inherit the first's cursor and its assertions would
    depend on collection order — so each test starts and ends by wiping it.
    """
    async with EventBus(nats_url) as connected:
        await _drop_stream(connected)
        yield connected
        await _drop_stream(connected)


async def _drop_stream(bus: EventBus) -> None:
    with contextlib.suppress(NotFoundError):
        await bus.jetstream().delete_stream(EVENTS_STREAM)


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[EventLogStore]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    store = EventLogStore(engine)
    await store.init()
    yield store
    # Dispose before the test's loop closes: aiosqlite connections each own a worker
    # thread, and an undisposed engine leaves them raising "Event loop is closed" at GC.
    await engine.dispose()


async def _wait_for_count(store: EventLogStore, expected: int, *, timeout: float = 10.0) -> int:
    """Poll until the log holds *expected* rows, or give up — delivery is asynchronous."""

    async def _poll() -> int:
        while True:
            count = await store.count()
            if count >= expected:
                return count
            await asyncio.sleep(0.05)

    try:
        return await asyncio.wait_for(_poll(), timeout=timeout)
    except TimeoutError:
        return await store.count()


async def _started(store: EventLogStore, bus: EventBus, **kwargs: float) -> EventIntake:
    intake = EventIntake(store, bus, ack_wait_s=kwargs.get("ack_wait_s", ACK_WAIT_S))
    await intake.start()
    return intake


# ── routing ──────────────────────────────────────────────────────────────────


async def test_emit_reaches_the_durable_log_over_the_wire(
    bus: EventBus, store: EventLogStore
) -> None:
    """The chain the whole spine exists for: emit → NATS → intake → durable log."""
    intake = await _started(store, bus)
    await emit_event(
        bus,
        tenant_id="local",
        module="echo",
        event_type="echo.pinged",
        dedup_key="wire-1",
        payload={"note": "hello"},
    )
    assert await _wait_for_count(store, 1) == 1
    await intake.stop()

    rows = await store.recent(tenant="local")
    assert [r.dedup_key for r in rows] == ["wire-1"]
    assert rows[0].payload == {"note": "hello"}
    assert rows[0].type == "echo.pinged"


async def test_the_wildcard_spans_every_tenant(bus: EventBus, store: EventLogStore) -> None:
    """One consumer, every tenant — the reason intake does not take a tenant list.

    A per-tenant consumer set would record the first tenant and silently ignore the
    second, which is indistinguishable from "the second tenant emitted nothing".
    """
    intake = await _started(store, bus)
    for tenant in ("local", "second-tenant"):
        await emit_event(
            bus,
            tenant_id=tenant,
            module="echo",
            event_type="echo.pinged",
            dedup_key=f"{tenant}-1",
        )
    assert await _wait_for_count(store, 2) == 2
    await intake.stop()

    assert [r.dedup_key for r in await store.recent(tenant="local")] == ["local-1"]
    assert [r.dedup_key for r in await store.recent(tenant="second-tenant")] == ["second-tenant-1"]


async def test_the_wildcard_ignores_non_spine_traffic(bus: EventBus, store: EventLogStore) -> None:
    """``*.events.>`` must not swallow the bus's existing per-module subjects.

    ``notes.saved``, ``llm.usage``, and ``echo.request`` all share the tenant-scoped shape
    but are not envelopes; a wildcard that caught them would fill the log — and now the
    *stream* — with parse failures. This is why the spine took its own ``events.``
    namespace instead of matching ``*.>``.
    """
    intake = await _started(store, bus)
    await bus.publish("notes.saved", {"slug": "a-note"}, tenant_id="local")
    await bus.publish("llm.usage", {"tokens": 10}, tenant_id="local")
    await bus.client.flush()
    # Then a real event, so we are asserting "only this one" rather than "nothing yet".
    await emit_event(
        bus,
        tenant_id="local",
        module="echo",
        event_type="echo.pinged",
        dedup_key="only-me",
    )
    assert await _wait_for_count(store, 1) == 1
    await asyncio.sleep(0.2)  # give any stray delivery time to arrive and be wrong
    await intake.stop()

    rows = await store.recent(tenant="local")
    assert [r.dedup_key for r in rows] == ["only-me"]
    # And the stream itself never took them: a persisted log of non-envelopes would be a
    # redelivery loop, not just noise.
    info = await bus.jetstream().stream_info(EVENTS_STREAM)
    assert info.state.messages == 1


async def test_duplicate_emission_is_stored_once_over_the_wire(
    bus: EventBus, store: EventLogStore
) -> None:
    """The acceptance criterion, end to end: same dedup_key twice → one row."""
    intake = await _started(store, bus)
    for _ in range(2):
        await emit_event(
            bus,
            tenant_id="local",
            module="echo",
            event_type="echo.pinged",
            dedup_key="same-change",
        )
    assert await _wait_for_count(store, 1) == 1
    await asyncio.sleep(0.5)  # let the second delivery land and be rejected
    await intake.stop()

    assert await store.count() == 1


async def test_a_live_listener_sees_the_event(bus: EventBus, store: EventLogStore) -> None:
    """The seam the automations engine plugs into, proven over the wire."""
    seen: list[str] = []
    heard = asyncio.Event()

    async def _listener(entry: object) -> None:
        seen.append(getattr(entry, "dedup_key", ""))
        heard.set()

    intake = EventIntake(store, bus, ack_wait_s=ACK_WAIT_S)
    intake.on_event(_listener)
    await intake.start()

    await emit_event(
        bus,
        tenant_id="local",
        module="echo",
        event_type="echo.pinged",
        dedup_key="notify-me",
    )
    await asyncio.wait_for(heard.wait(), timeout=10)
    await intake.stop()

    assert seen == ["notify-me"]


# ── durability: the point of the promotion ───────────────────────────────────


async def test_an_event_emitted_while_the_core_is_down_is_not_lost(
    bus: EventBus, store: EventLogStore
) -> None:
    """The failure #832 exists to close: nothing is consuming when the world changes.

    Nothing subscribes at emit time here — the stream is provisioned, the consumer is not.
    Under the old core-NATS transport this event was simply gone; now it is waiting when
    intake comes up.
    """
    await bus.ensure_stream(
        EVENTS_STREAM,
        ["*.events.>"],
        max_age_s=3600.0,
        max_bytes=64 * 1024 * 1024,
    )
    await emit_event(
        bus,
        tenant_id="local",
        module="echo",
        event_type="echo.pinged",
        dedup_key="while-you-were-out",
    )
    await bus.client.flush()

    intake = await _started(store, bus)
    assert await _wait_for_count(store, 1) == 1
    await intake.stop()
    assert [r.dedup_key for r in await store.recent(tenant="local")] == ["while-you-were-out"]


async def test_a_restart_mid_stream_loses_nothing_and_records_nothing_twice(
    bus: EventBus, store: EventLogStore
) -> None:
    """Kill the consumer partway through a burst, bring it back, and count.

    A slowed store guarantees the stop actually lands *mid-stream* rather than after the
    last message, which is the only version of this test that proves anything. The second
    intake binds the same durable, so it must pick up the tail — and the ones the first
    intake already committed must not be recorded again.
    """
    total = 8
    real_append = store.append

    async def _slow_append(envelope: object) -> object:
        await asyncio.sleep(0.05)
        return await real_append(envelope)  # type: ignore[arg-type]

    store.append = _slow_append  # type: ignore[method-assign, assignment]

    await bus.ensure_stream(
        EVENTS_STREAM, ["*.events.>"], max_age_s=3600.0, max_bytes=64 * 1024 * 1024
    )
    for i in range(total):
        await emit_event(
            bus,
            tenant_id="local",
            module="echo",
            event_type="echo.pinged",
            dedup_key=f"burst-{i}",
        )
    await bus.client.flush()

    first = await _started(store, bus)
    partial = await _wait_for_count(store, 1, timeout=10.0)
    assert partial >= 1
    await first.stop()  # the core "dies" with the burst half-consumed
    stopped_at = await store.count()
    assert stopped_at < total, "the store was not slow enough to stop mid-stream"

    second = await _started(store, bus)
    assert await _wait_for_count(store, total, timeout=30.0) == total
    await second.stop()

    rows = await store.recent(tenant="local", limit=total * 2)
    assert sorted(r.dedup_key for r in rows) == sorted(f"burst-{i}" for i in range(total))
    assert await store.count() == total  # nothing lost, nothing double-recorded


async def test_an_unacked_event_is_redelivered_and_still_recorded_once(
    bus: EventBus, store: EventLogStore
) -> None:
    """The row is committed, the ack never lands — the crash window at-least-once covers.

    JetStream hands the message back after ``ack_wait``; the redelivery re-runs ``append``,
    loses to the unique constraint, and is acked as a no-op. Exactly one row, and the
    listener seam does not fire twice.
    """
    seen: list[str] = []

    class _AckDroppingIntake(EventIntake):
        """Commits the row, then drops the first ack on the floor."""

        dropped = 0
        acked = 0

        async def _ack(self, msg: Msg) -> None:
            if self.dropped == 0:
                self.dropped += 1
                return
            self.acked += 1
            await super()._ack(msg)

    async def _listener(entry: object) -> None:
        seen.append(getattr(entry, "dedup_key", ""))

    intake = _AckDroppingIntake(store, bus, ack_wait_s=ACK_WAIT_S)
    intake.on_event(_listener)
    await intake.start()
    try:
        await emit_event(
            bus,
            tenant_id="local",
            module="echo",
            event_type="echo.pinged",
            dedup_key="unacked-once",
        )
        assert await _wait_for_count(store, 1) == 1
        # Now wait past ack_wait for the redelivery, and for the second (real) ack.
        for _ in range(int(ACK_WAIT_S * 4 / 0.1)):
            if intake.acked:
                break
            await asyncio.sleep(0.1)
        assert intake.acked >= 1, "the unacked message never came back"
    finally:
        await intake.stop()

    assert await store.count() == 1  # the redelivery deduped rather than duplicating
    assert seen == ["unacked-once"]  # and a consumer never saw it twice


async def test_a_malformed_message_is_terminated_and_does_not_loop(
    bus: EventBus, store: EventLogStore
) -> None:
    """Unlimited redelivery needs an escape hatch, or one bad emit spins forever.

    Published raw onto a spine subject, so it reaches intake as a real delivery. It can
    never parse, so it must be refused permanently — and the good event behind it must
    still get through.
    """
    intake = await _started(store, bus)
    await bus.publish("events.echo.pinged", b"not an envelope", tenant_id="local")
    await bus.client.flush()
    await emit_event(
        bus,
        tenant_id="local",
        module="echo",
        event_type="echo.pinged",
        dedup_key="behind-the-bad-one",
    )
    assert await _wait_for_count(store, 1) == 1
    # Past ack_wait: a naked or merely-unacked message would be back by now.
    await asyncio.sleep(ACK_WAIT_S * 1.5)
    consumer = await bus.jetstream().consumer_info(EVENTS_STREAM, "core-event-intake")
    assert consumer.num_redelivered == 0
    assert consumer.num_pending == 0
    await intake.stop()

    assert await store.count() == 1
