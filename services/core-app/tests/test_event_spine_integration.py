"""Integration test: the module event spine over real NATS. Requires Docker.

The unit tests drive intake with a fake bus, which will happily accept any subject string
— including one a real broker would reject or route differently. The one thing only a real
server can prove is the subscription itself: that ``*.events.>`` actually matches a
tenant-scoped envelope subject, across *every* tenant, and nothing else.

That matters because the failure mode is silent. A wildcard that does not match produces
no error, no log, and no event — an intake that appears healthy and records nothing.

The store is a *file-backed* SQLite, not the in-memory + ``StaticPool`` one the unit tests
use. Here the intake writes from the NATS callback task while the test polls ``count()``
from its own task, and ``StaticPool`` hands both sessions the *same* DBAPI connection —
the pool's reset-``ROLLBACK`` on each poll checkout can then land inside ``append``'s
``BEGIN…COMMIT`` and silently erase the insert (the append still returns a row, and the
next insert re-uses its id). A file database gives every session its own connection, which
is also what production Postgres does; the unit tests keep the in-memory store because
they never touch it from two tasks at once.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

from epicurus_core import EventBus, emit_event
from epicurus_core_app.event_log import EventIntake, EventLogStore

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def nats_url() -> Iterator[str]:
    container = DockerContainer("nats:2.10").with_command("-js").with_exposed_ports(4222)
    with container:
        wait_for_logs(container, "Server is ready")
        yield f"nats://{container.get_container_host_ip()}:{container.get_exposed_port(4222)}"


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[EventLogStore]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    store = EventLogStore(engine)
    await store.init()
    yield store
    # Dispose before the test's loop closes: aiosqlite connections each own a worker
    # thread, and an undisposed engine leaves them raising "Event loop is closed" at GC.
    await engine.dispose()


async def _wait_for_count(store: EventLogStore, expected: int, *, timeout: float = 5.0) -> int:
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


async def test_emit_reaches_the_durable_log_over_the_wire(
    nats_url: str, store: EventLogStore
) -> None:
    """The chain the whole spine exists for: emit → NATS → intake → durable log."""
    async with EventBus(nats_url) as bus:
        intake = EventIntake(store, bus)
        await intake.start()
        await bus.client.flush()

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


async def test_the_wildcard_spans_every_tenant(nats_url: str, store: EventLogStore) -> None:
    """One subscription, every tenant — the reason intake does not take a tenant list.

    A per-tenant subscription set would record the first tenant and silently ignore the
    second, which is indistinguishable from "the second tenant emitted nothing".
    """
    async with EventBus(nats_url) as bus:
        intake = EventIntake(store, bus)
        await intake.start()
        await bus.client.flush()

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


async def test_the_wildcard_ignores_non_spine_traffic(nats_url: str, store: EventLogStore) -> None:
    """``*.events.>`` must not swallow the bus's existing per-module subjects.

    ``notes.saved``, ``llm.usage``, and ``echo.request`` all share the tenant-scoped shape
    but are not envelopes; a wildcard that caught them would fill the log with parse
    failures. This is why the spine took its own ``events.`` namespace instead of matching
    ``*.>``.
    """
    async with EventBus(nats_url) as bus:
        intake = EventIntake(store, bus)
        await intake.start()
        await bus.client.flush()

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


async def test_duplicate_emission_is_stored_once_over_the_wire(
    nats_url: str, store: EventLogStore
) -> None:
    """The acceptance criterion, end to end: same dedup_key twice → one row."""
    async with EventBus(nats_url) as bus:
        intake = EventIntake(store, bus)
        await intake.start()
        await bus.client.flush()

        for _ in range(2):
            await emit_event(
                bus,
                tenant_id="local",
                module="echo",
                event_type="echo.pinged",
                dedup_key="same-change",
            )
        assert await _wait_for_count(store, 1) == 1
        await asyncio.sleep(0.2)  # let the second delivery land and be rejected
        await intake.stop()

    assert await store.count() == 1


async def test_a_live_listener_sees_the_event(nats_url: str, store: EventLogStore) -> None:
    """The seam the automations engine plugs into, proven over the wire."""
    seen: list[str] = []
    heard = asyncio.Event()

    async def _listener(entry: object) -> None:
        seen.append(getattr(entry, "dedup_key", ""))
        heard.set()

    async with EventBus(nats_url) as bus:
        intake = EventIntake(store, bus)
        intake.on_event(_listener)
        await intake.start()
        await bus.client.flush()

        await emit_event(
            bus,
            tenant_id="local",
            module="echo",
            event_type="echo.pinged",
            dedup_key="notify-me",
        )
        await asyncio.wait_for(heard.wait(), timeout=5)
        await intake.stop()

    assert seen == ["notify-me"]
