"""Integration tests for the bus's JetStream primitives. Require Docker (testcontainers).

Everything here needs a real broker to mean anything. A fake can be told that a stream
exists, that a durable resumed, or that an unacked message came back — a server is the only
thing that can be *asked*. Three properties in particular are load-bearing for the module
event spine (ADR-0103 §4 as amended) and silent when they break:

* **A plain, unacknowledged publish still lands in the stream.** This is the entire basis
  for leaving every module emitter untouched: persistence is a property of the subject, not
  of the publisher's API. If it were false, the spine would look healthy and store nothing.
* **A durable consumer resumes at its own cursor.** A rebind that silently started over
  would replay the world on every restart; one that silently skipped ahead would lose it.
* **An unacked message comes back.** That is at-least-once, and it is the one behavior the
  core's ack-after-commit ordering is worth writing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Iterator

import nats.errors
import pytest
from nats.js.api import StorageType, StreamConfig
from nats.js.errors import APIError, NotFoundError
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

from epicurus_core.events import EventBus

pytestmark = pytest.mark.integration

STREAM = "TEST_EVENTS"
SUBJECT = "*.events.>"
DURABLE = "test-intake"

# Short enough that a redelivery happens inside a test, long enough that a healthy handler
# is never mistaken for a dead one on a loaded CI box.
ACK_WAIT_S = 1.0


@pytest.fixture(scope="module")
def nats_url() -> Iterator[str]:
    container = DockerContainer("nats:2.10").with_command("-js").with_exposed_ports(4222)
    with container:
        wait_for_logs(container, "Server is ready")
        host = container.get_container_host_ip()
        port = container.get_exposed_port(4222)
        yield f"nats://{host}:{port}"


@pytest.fixture
async def bus(nats_url: str) -> AsyncIterator[EventBus]:
    """A connected bus on a server with no stream named :data:`STREAM`.

    The stream and its durables are server-side state that outlives a test, so each test
    starts by wiping it — otherwise the second test to run would inherit the first's cursor
    and its assertions would depend on collection order.
    """
    async with EventBus(nats_url) as connected:
        await _drop_stream(connected)
        yield connected
        await _drop_stream(connected)


async def _drop_stream(bus: EventBus) -> None:
    with contextlib.suppress(NotFoundError):
        await bus.jetstream().delete_stream(STREAM)


async def _ensure(bus: EventBus) -> None:
    await bus.ensure_stream(STREAM, [SUBJECT], max_age_s=3600.0, max_bytes=1024 * 1024)


def _value(field: object) -> object:
    """A ``StreamConfig`` field's value, whether nats-py handed back an enum or its string.

    ``add_stream`` echoes the server's JSON without coercing the enums back, so the config
    that comes *out* holds ``'file'`` where the one that went *in* held ``StorageType.FILE``.
    """
    return getattr(field, "value", field)


async def _wait_for_messages(bus: EventBus, expected: int, *, timeout: float = 3.0) -> int:
    """Poll the stream's own count — a flushed PUB is *received*, not necessarily stored."""
    deadline = asyncio.get_running_loop().time() + timeout
    count = 0
    while asyncio.get_running_loop().time() < deadline:
        count = (await bus.jetstream().stream_info(STREAM)).state.messages
        if count >= expected:
            return count
        await asyncio.sleep(0.05)
    return count


# ── provisioning ─────────────────────────────────────────────────────────────


async def test_ensure_stream_creates_the_stream(bus: EventBus) -> None:
    await _ensure(bus)
    info = await bus.jetstream().stream_info(STREAM)
    assert info.config.subjects == [SUBJECT]
    # `limits` retention, not workqueue/interest: a message must survive whether or not a
    # consumer exists at the moment it is published — that window is the point.
    assert _value(info.config.retention) == "limits"
    assert _value(info.config.discard) == "old"
    assert _value(info.config.storage) == "file"
    # Forced, not chosen: a `*`-leading subject overlaps NATS's own `$JS.>` namespace, and
    # the server refuses such a stream unless publisher acks are off. See ensure_stream.
    assert info.config.no_ack is True


async def test_ensure_stream_is_idempotent(bus: EventBus) -> None:
    # Called on every boot, so "already there" is the normal path, not an error.
    await _ensure(bus)
    await _ensure(bus)
    await _ensure(bus)
    names = [s.config.name for s in await bus.jetstream().streams_info()]
    assert names.count(STREAM) == 1


async def test_ensure_stream_widens_an_existing_stream(bus: EventBus) -> None:
    # An upgrade that adds a subject must adopt the existing stream, not refuse to boot
    # beside it — two streams cannot overlap on a subject, so "create a new one" is not an
    # option the server would even allow.
    await bus.jetstream().add_stream(
        StreamConfig(
            name=STREAM, subjects=["*.events.echo.>"], storage=StorageType.FILE, no_ack=True
        )
    )
    await _ensure(bus)
    info = await bus.jetstream().stream_info(STREAM)
    assert info.config.subjects == [SUBJECT]


async def test_ensure_stream_keeps_a_stream_whose_config_cannot_be_updated(
    bus: EventBus,
) -> None:
    """An immutable-field mismatch is a warning, not a boot failure.

    Storage type cannot be changed on an existing stream. An operator who provisioned this
    stream in memory has a *worse* stream than we would have made, but a working one — and
    refusing to start the core over a tuning difference trades a degraded spine for no
    spine at all.
    """
    await bus.jetstream().add_stream(
        StreamConfig(name=STREAM, subjects=[SUBJECT], storage=StorageType.MEMORY, no_ack=True)
    )
    await _ensure(bus)  # must not raise
    info = await bus.jetstream().stream_info(STREAM)
    assert _value(info.config.storage) == "memory"
    assert info.config.subjects == [SUBJECT]


async def test_ensure_stream_raises_when_the_existing_stream_routes_elsewhere(
    bus: EventBus,
) -> None:
    # The one case that must be loud: the stream exists, cannot be updated, and does not
    # carry our subjects. Every event would land nowhere, with nothing in the logs to say
    # so — exactly the silent failure the durable transport is meant to end.
    await bus.jetstream().add_stream(
        StreamConfig(
            name=STREAM, subjects=["*.elsewhere.>"], storage=StorageType.MEMORY, no_ack=True
        )
    )
    with pytest.raises(APIError):
        await _ensure(bus)


# ── the publish side ─────────────────────────────────────────────────────────


async def test_a_plain_publish_is_captured_by_the_stream(bus: EventBus) -> None:
    """The decision this whole design rests on: `bus.publish` needs no JetStream ack.

    A stream captures whatever lands on its subjects, whoever published it. If this were
    false, every module emitter would have to change — and the spine would have become a
    contract change rather than a transport change.
    """
    await _ensure(bus)
    await bus.publish("events.echo.pinged", {"note": "hi"}, tenant_id="local")
    await bus.client.flush()

    assert await _wait_for_messages(bus, 1) == 1


async def test_an_event_published_before_any_consumer_exists_is_still_delivered(
    bus: EventBus,
) -> None:
    """The failure the promotion exists to remove: emitted while the core was down.

    The consumer here is created *after* the publish, which is exactly the shape of a core
    that was restarting when a module announced something.
    """
    await _ensure(bus)
    await bus.publish("events.echo.pinged", {"note": "while you were out"}, tenant_id="local")
    await bus.client.flush()

    sub = await bus.pull_subscribe_any_tenant(
        "events.>", durable=DURABLE, stream=STREAM, ack_wait_s=ACK_WAIT_S
    )
    msgs = await sub.fetch(batch=5, timeout=2.0)
    assert [m.subject for m in msgs] == ["local.events.echo.pinged"]
    for msg in msgs:
        await msg.ack()


# ── the durable consumer ─────────────────────────────────────────────────────


async def test_pull_subscribe_any_tenant_spans_every_tenant(bus: EventBus) -> None:
    # One consumer, every tenant (constraint #1): a per-tenant consumer list would silently
    # ignore a tenant created after boot.
    await _ensure(bus)
    sub = await bus.pull_subscribe_any_tenant(
        "events.>", durable=DURABLE, stream=STREAM, ack_wait_s=ACK_WAIT_S
    )
    for tenant in ("local", "second-tenant"):
        await bus.publish("events.echo.pinged", {"t": tenant}, tenant_id=tenant)
    await bus.client.flush()

    seen: list[str] = []
    while len(seen) < 2:
        for msg in await sub.fetch(batch=5, timeout=2.0):
            seen.append(msg.subject)
            await msg.ack()
    assert sorted(seen) == ["local.events.echo.pinged", "second-tenant.events.echo.pinged"]


async def test_the_durable_cursor_survives_a_rebind(bus: EventBus) -> None:
    """A restarted consumer resumes where its acks left off — it neither replays nor skips."""
    await _ensure(bus)
    sub = await bus.pull_subscribe_any_tenant(
        "events.>", durable=DURABLE, stream=STREAM, ack_wait_s=ACK_WAIT_S
    )
    await bus.publish("events.echo.pinged", {"n": 1}, tenant_id="local")
    await bus.client.flush()
    first = await sub.fetch(batch=5, timeout=2.0)
    assert len(first) == 1
    await first[0].ack()
    await sub.unsubscribe()

    # …the consumer goes away, and the world keeps changing while it is gone.
    await bus.publish("events.echo.pinged", {"n": 2}, tenant_id="local")
    await bus.client.flush()

    rebound = await bus.pull_subscribe_any_tenant(
        "events.>", durable=DURABLE, stream=STREAM, ack_wait_s=ACK_WAIT_S
    )
    second = await rebound.fetch(batch=5, timeout=2.0)
    assert [json.loads(m.data) for m in second] == [{"n": 2}]  # not the already-acked n=1
    await second[0].ack()


async def test_an_unacked_message_comes_back(bus: EventBus) -> None:
    """At-least-once, in one assertion: no ack, so the server hands it back."""
    await _ensure(bus)
    sub = await bus.pull_subscribe_any_tenant(
        "events.>", durable=DURABLE, stream=STREAM, ack_wait_s=ACK_WAIT_S
    )
    await bus.publish("events.echo.pinged", {"n": 1}, tenant_id="local")
    await bus.client.flush()

    first = await sub.fetch(batch=1, timeout=2.0)
    assert first[0].metadata.num_delivered == 1
    # …and here the consumer "dies": no ack, no nak, nothing.
    await asyncio.sleep(ACK_WAIT_S * 1.5)

    again = await sub.fetch(batch=1, timeout=3.0)
    assert again[0].data == first[0].data
    assert again[0].metadata.num_delivered == 2
    await again[0].ack()


async def test_a_terminated_message_never_comes_back(bus: EventBus) -> None:
    # The escape hatch that keeps unlimited redelivery from becoming an infinite loop: a
    # message that can never be stored is refused permanently, not retried forever.
    await _ensure(bus)
    sub = await bus.pull_subscribe_any_tenant(
        "events.>", durable=DURABLE, stream=STREAM, ack_wait_s=ACK_WAIT_S
    )
    await bus.publish("events.echo.pinged", b"not an envelope", tenant_id="local")
    await bus.client.flush()

    first = await sub.fetch(batch=1, timeout=2.0)
    await first[0].term()

    await asyncio.sleep(ACK_WAIT_S * 1.5)
    with pytest.raises(nats.errors.TimeoutError):
        await sub.fetch(batch=1, timeout=1.0)
