"""Integration tests for the NATS EventBus. Require Docker (testcontainers)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from structlog.testing import capture_logs
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

from epicurus_core.events import Event, EventBus, Payload

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def nats_url() -> Iterator[str]:
    container = DockerContainer("nats:2.10").with_command("-js").with_exposed_ports(4222)
    with container:
        wait_for_logs(container, "Server is ready")
        host = container.get_container_host_ip()
        port = container.get_exposed_port(4222)
        yield f"nats://{host}:{port}"


async def test_publish_subscribe(nats_url: str) -> None:
    received: asyncio.Queue[Event] = asyncio.Queue()

    async with EventBus(nats_url) as bus:
        await bus.subscribe("demo.topic", received.put, tenant_id="acme")
        await bus.client.flush()
        await bus.publish("demo.topic", {"hello": "world"}, tenant_id="acme")
        event = await asyncio.wait_for(received.get(), timeout=2)

    assert event.subject == "acme.demo.topic"
    assert event.json() == {"hello": "world"}


async def test_request_reply(nats_url: str) -> None:
    async def echo(event: Event) -> bytes:
        return b"pong:" + event.data

    async with EventBus(nats_url) as bus:
        await bus.reply("demo.ping", echo, tenant_id="acme")
        await bus.client.flush()
        response = await bus.request("demo.ping", b"ping", tenant_id="acme")

    assert response.text == "pong:ping"


async def test_handler_exception_is_logged_and_subscription_survives(nats_url: str) -> None:
    received: asyncio.Queue[Event] = asyncio.Queue()

    async def flaky(event: Event) -> None:
        if event.data == b"boom":
            raise RuntimeError("boom")
        await received.put(event)

    async with EventBus(nats_url) as bus:
        await bus.subscribe("demo.flaky", flaky, tenant_id="acme")
        await bus.client.flush()
        with capture_logs() as logs:
            await bus.publish("demo.flaky", b"boom", tenant_id="acme")
            await bus.publish("demo.flaky", b"ok", tenant_id="acme")
            # Delivery is in-order: receiving "ok" proves "boom" was handled first.
            event = await asyncio.wait_for(received.get(), timeout=2)

    assert event.data == b"ok"
    assert any(entry["event"] == "event handler raised" for entry in logs)


async def test_replier_exception_is_logged_and_later_requests_succeed(nats_url: str) -> None:
    calls = 0

    async def flaky_replier(event: Event) -> Payload:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("first call only")
        return b"recovered"

    async with EventBus(nats_url) as bus:
        await bus.reply("demo.flaky-reply", flaky_replier, tenant_id="acme")
        await bus.client.flush()

        # The raising replier sends no response — the requester times out.
        with capture_logs() as logs, pytest.raises(TimeoutError):
            await bus.request("demo.flaky-reply", b"one", timeout=0.5, tenant_id="acme")

        # The subscription survives and serves the next request.
        response = await bus.request("demo.flaky-reply", b"two", timeout=2, tenant_id="acme")

    assert response.data == b"recovered"
    assert any("replier raised" in entry["event"] for entry in logs)


async def test_tenant_isolation(nats_url: str) -> None:
    received: asyncio.Queue[Event] = asyncio.Queue()

    async with EventBus(nats_url) as bus:
        await bus.subscribe("demo.topic", received.put, tenant_id="acme")
        await bus.client.flush()

        # A different tenant's message must NOT reach the acme subscriber.
        await bus.publish("demo.topic", b"x", tenant_id="other")
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(received.get(), timeout=0.5)

        # The same tenant's message is delivered.
        await bus.publish("demo.topic", b"y", tenant_id="acme")
        event = await asyncio.wait_for(received.get(), timeout=2)

    assert event.subject == "acme.demo.topic"
    assert event.data == b"y"
