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

**Isolation is by namespace, not by wiping.** Until #919 every test here shared one stream
name, one durable and one subject, and the fixture deleted and re-created that stream around
each test. Three CI runs then failed with another test's messages in hand — twice
``test_an_unacked_message_comes_back`` fetched ``{"n": 2}`` from the rebind test above it,
and once ``test_an_event_published_before_any_consumer_exists_is_still_delivered`` got its
own publish *twice*, the extra copy being the previous test's identical
``local.events.echo.pinged``. Deleting a file-backed JetStream stream and immediately
re-creating it under the same name does not reliably give you an empty one; and a wipe can
only ever be as reliable as the server makes it. So each test now gets its own stream,
durable and subject prefix (:class:`Namespace`), and nothing it does is addressable by any
other test — the leak is impossible rather than cleaned up. The teardown drop that remains
is housekeeping on a shared container, not a correctness measure.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

import nats.errors
import pytest
from nats.js import JetStreamContext
from nats.js.api import StorageType, StreamConfig
from nats.js.errors import APIError, NotFoundError
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

from epicurus_core.events import EventBus

pytestmark = pytest.mark.integration

# Short enough that a redelivery happens inside a test, long enough that a healthy handler
# is never mistaken for a dead one on a loaded CI box.
ACK_WAIT_S = 1.0


@dataclass(frozen=True)
class Namespace:
    """One test's private corner of the shared NATS server.

    The leading ``*`` of :attr:`subject` is not incidental — it is where
    :func:`~epicurus_core.tenancy.scope_subject` puts the tenant, and a stream whose
    subject starts with a wildcard is the reason ``ensure_stream`` must force ``no_ack``.
    The per-test token goes in the *second* token so that shape is preserved exactly.
    """

    token: str

    @property
    def stream(self) -> str:
        return f"TEST_EVENTS_{self.token}"

    @property
    def durable(self) -> str:
        return f"test-intake-{self.token}"

    @property
    def base(self) -> str:
        """The unscoped subject prefix this test publishes under."""
        return f"events.{self.token}"

    @property
    def subject(self) -> str:
        """What the stream captures: every tenant's copy of this test's subtree."""
        return f"*.{self.base}.>"

    @property
    def pinged(self) -> str:
        """The unscoped subject a module-style emitter would publish on."""
        return f"{self.base}.echo.pinged"

    def scoped(self, tenant: str) -> str:
        """:attr:`pinged` as it appears on the wire for *tenant*."""
        return f"{tenant}.{self.pinged}"


@pytest.fixture(scope="module")
def nats_url() -> Iterator[str]:
    container = DockerContainer("nats:2.10").with_command("-js").with_exposed_ports(4222)
    with container:
        wait_for_logs(container, "Server is ready")
        host = container.get_container_host_ip()
        port = container.get_exposed_port(4222)
        yield f"nats://{host}:{port}"


@pytest.fixture
def ns() -> Namespace:
    """Names nothing else in this module (or a parallel run) can address."""
    return Namespace(uuid.uuid4().hex[:10])


@pytest.fixture
async def bus(nats_url: str, ns: Namespace) -> AsyncIterator[EventBus]:
    """A connected bus whose stream name and subjects belong to this test alone."""
    async with EventBus(nats_url) as connected:
        yield connected
        # Flush before dropping: a publish this test made may still be in the client's
        # write buffer, and deleting the stream under it would leave the message with
        # nowhere to land — the shape of the cross-test bleed this namespacing replaced.
        with contextlib.suppress(Exception):
            await connected.client.flush()
        await _drop_stream(connected, ns)


async def _drop_stream(bus: EventBus, ns: Namespace) -> None:
    with contextlib.suppress(NotFoundError):
        await bus.jetstream().delete_stream(ns.stream)


async def _ensure(bus: EventBus, ns: Namespace) -> None:
    await bus.ensure_stream(ns.stream, [ns.subject], max_age_s=3600.0, max_bytes=1024 * 1024)


async def _subscribe(bus: EventBus, ns: Namespace) -> JetStreamContext.PullSubscription:
    return await bus.pull_subscribe_any_tenant(
        f"{ns.base}.>", durable=ns.durable, stream=ns.stream, ack_wait_s=ACK_WAIT_S
    )


def _value(field: object) -> object:
    """A ``StreamConfig`` field's value, whether nats-py handed back an enum or its string.

    ``add_stream`` echoes the server's JSON without coercing the enums back, so the config
    that comes *out* holds ``'file'`` where the one that went *in* held ``StorageType.FILE``.
    """
    return getattr(field, "value", field)


async def _wait_for_messages(
    bus: EventBus, ns: Namespace, expected: int, *, timeout: float = 3.0
) -> int:
    """Poll the stream's own count — a flushed PUB is *received*, not necessarily stored."""
    deadline = asyncio.get_running_loop().time() + timeout
    count = 0
    while asyncio.get_running_loop().time() < deadline:
        count = (await bus.jetstream().stream_info(ns.stream)).state.messages
        if count >= expected:
            return count
        await asyncio.sleep(0.05)
    return count


# ── provisioning ─────────────────────────────────────────────────────────────


async def test_ensure_stream_creates_the_stream(bus: EventBus, ns: Namespace) -> None:
    await _ensure(bus, ns)
    info = await bus.jetstream().stream_info(ns.stream)
    assert info.config.subjects == [ns.subject]
    # `limits` retention, not workqueue/interest: a message must survive whether or not a
    # consumer exists at the moment it is published — that window is the point.
    assert _value(info.config.retention) == "limits"
    assert _value(info.config.discard) == "old"
    assert _value(info.config.storage) == "file"
    # Forced, not chosen: a `*`-leading subject overlaps NATS's own `$JS.>` namespace, and
    # the server refuses such a stream unless publisher acks are off. See ensure_stream.
    assert info.config.no_ack is True


async def test_ensure_stream_is_idempotent(bus: EventBus, ns: Namespace) -> None:
    # Called on every boot, so "already there" is the normal path, not an error.
    await _ensure(bus, ns)
    await _ensure(bus, ns)
    await _ensure(bus, ns)
    names = [s.config.name for s in await bus.jetstream().streams_info()]
    assert names.count(ns.stream) == 1


async def test_ensure_stream_widens_an_existing_stream(bus: EventBus, ns: Namespace) -> None:
    # An upgrade that adds a subject must adopt the existing stream, not refuse to boot
    # beside it — two streams cannot overlap on a subject, so "create a new one" is not an
    # option the server would even allow.
    await bus.jetstream().add_stream(
        StreamConfig(
            name=ns.stream,
            subjects=[f"*.{ns.base}.echo.>"],
            storage=StorageType.FILE,
            no_ack=True,
        )
    )
    await _ensure(bus, ns)
    info = await bus.jetstream().stream_info(ns.stream)
    assert info.config.subjects == [ns.subject]


async def test_ensure_stream_keeps_a_stream_whose_config_cannot_be_updated(
    bus: EventBus, ns: Namespace
) -> None:
    """An immutable-field mismatch is a warning, not a boot failure.

    Storage type cannot be changed on an existing stream. An operator who provisioned this
    stream in memory has a *worse* stream than we would have made, but a working one — and
    refusing to start the core over a tuning difference trades a degraded spine for no
    spine at all.
    """
    await bus.jetstream().add_stream(
        StreamConfig(name=ns.stream, subjects=[ns.subject], storage=StorageType.MEMORY, no_ack=True)
    )
    await _ensure(bus, ns)  # must not raise
    info = await bus.jetstream().stream_info(ns.stream)
    assert _value(info.config.storage) == "memory"
    assert info.config.subjects == [ns.subject]


async def test_ensure_stream_raises_when_the_existing_stream_routes_elsewhere(
    bus: EventBus, ns: Namespace
) -> None:
    # The one case that must be loud: the stream exists, cannot be updated, and does not
    # carry our subjects. Every event would land nowhere, with nothing in the logs to say
    # so — exactly the silent failure the durable transport is meant to end.
    await bus.jetstream().add_stream(
        StreamConfig(
            name=ns.stream,
            subjects=[f"*.{ns.token}.elsewhere.>"],
            storage=StorageType.MEMORY,
            no_ack=True,
        )
    )
    with pytest.raises(APIError):
        await _ensure(bus, ns)


# ── the publish side ─────────────────────────────────────────────────────────


async def test_a_plain_publish_is_captured_by_the_stream(bus: EventBus, ns: Namespace) -> None:
    """The decision this whole design rests on: `bus.publish` needs no JetStream ack.

    A stream captures whatever lands on its subjects, whoever published it. If this were
    false, every module emitter would have to change — and the spine would have become a
    contract change rather than a transport change.
    """
    await _ensure(bus, ns)
    await bus.publish(ns.pinged, {"note": "hi"}, tenant_id="local")
    await bus.client.flush()

    assert await _wait_for_messages(bus, ns, 1) == 1


async def test_an_event_published_before_any_consumer_exists_is_still_delivered(
    bus: EventBus, ns: Namespace
) -> None:
    """The failure the promotion exists to remove: emitted while the core was down.

    The consumer here is created *after* the publish, which is exactly the shape of a core
    that was restarting when a module announced something.
    """
    await _ensure(bus, ns)
    await bus.publish(ns.pinged, {"note": "while you were out"}, tenant_id="local")
    await bus.client.flush()

    sub = await _subscribe(bus, ns)
    msgs = await sub.fetch(batch=5, timeout=2.0)
    assert [m.subject for m in msgs] == [ns.scoped("local")]
    for msg in msgs:
        await msg.ack()


# ── the durable consumer ─────────────────────────────────────────────────────


async def test_pull_subscribe_any_tenant_spans_every_tenant(bus: EventBus, ns: Namespace) -> None:
    # One consumer, every tenant (constraint #1): a per-tenant consumer list would silently
    # ignore a tenant created after boot.
    await _ensure(bus, ns)
    sub = await _subscribe(bus, ns)
    for tenant in ("local", "second-tenant"):
        await bus.publish(ns.pinged, {"t": tenant}, tenant_id=tenant)
    await bus.client.flush()

    seen: list[str] = []
    while len(seen) < 2:
        for msg in await sub.fetch(batch=5, timeout=2.0):
            seen.append(msg.subject)
            await msg.ack()
    assert sorted(seen) == sorted([ns.scoped("local"), ns.scoped("second-tenant")])


async def test_the_durable_cursor_survives_a_rebind(bus: EventBus, ns: Namespace) -> None:
    """A restarted consumer resumes where its acks left off — it neither replays nor skips."""
    await _ensure(bus, ns)
    sub = await _subscribe(bus, ns)
    await bus.publish(ns.pinged, {"n": 1}, tenant_id="local")
    await bus.client.flush()
    first = await sub.fetch(batch=5, timeout=2.0)
    assert len(first) == 1
    await first[0].ack()
    await sub.unsubscribe()

    # …the consumer goes away, and the world keeps changing while it is gone.
    await bus.publish(ns.pinged, {"n": 2}, tenant_id="local")
    await bus.client.flush()

    rebound = await _subscribe(bus, ns)
    second = await rebound.fetch(batch=5, timeout=2.0)
    assert [json.loads(m.data) for m in second] == [{"n": 2}]  # not the already-acked n=1
    await second[0].ack()


async def test_an_unacked_message_comes_back(bus: EventBus, ns: Namespace) -> None:
    """At-least-once, in one assertion: no ack, so the server hands it back."""
    await _ensure(bus, ns)
    sub = await _subscribe(bus, ns)
    await bus.publish(ns.pinged, {"n": 1}, tenant_id="local")
    await bus.client.flush()

    first = await sub.fetch(batch=1, timeout=2.0)
    assert json.loads(first[0].data) == {"n": 1}  # this test's own message, not a neighbour's
    assert first[0].metadata.num_delivered == 1
    # …and here the consumer "dies": no ack, no nak, nothing.
    await asyncio.sleep(ACK_WAIT_S * 1.5)

    again = await sub.fetch(batch=1, timeout=3.0)
    assert again[0].data == first[0].data
    assert again[0].metadata.num_delivered == 2
    await again[0].ack()


async def test_a_terminated_message_never_comes_back(bus: EventBus, ns: Namespace) -> None:
    # The escape hatch that keeps unlimited redelivery from becoming an infinite loop: a
    # message that can never be stored is refused permanently, not retried forever.
    await _ensure(bus, ns)
    sub = await _subscribe(bus, ns)
    await bus.publish(ns.pinged, b"not an envelope", tenant_id="local")
    await bus.client.flush()

    first = await sub.fetch(batch=1, timeout=2.0)
    await first[0].term()

    await asyncio.sleep(ACK_WAIT_S * 1.5)
    with pytest.raises(nats.errors.TimeoutError):
        await sub.fetch(batch=1, timeout=1.0)
