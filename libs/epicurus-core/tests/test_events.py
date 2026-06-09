"""Integration tests for the NATS EventBus. Require Docker (testcontainers)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

from epicurus_core.events import Event, EventBus

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
