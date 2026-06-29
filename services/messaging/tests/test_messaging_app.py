"""Integration tests: the messaging app's NATS wiring end-to-end. Requires Docker.

Boots the full app (its lifespan connects NATS, subscribes ``messaging.outbound``, and starts
the bridge) and proves both directions over a real NATS container:

* ``POST /loopback/inject`` → the bridge publishes ``messaging.inbound``;
* a published ``messaging.outbound`` → the bridge delivers it (visible as ``delivered`` in /status).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

from epicurus_core import (
    MESSAGING_INBOUND,
    MESSAGING_OUTBOUND,
    EventBus,
    InboundMessage,
    OutboundMessage,
)
from epicurus_messaging.app import create_app

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def nats_url() -> Iterator[str]:
    container = DockerContainer("nats:2.10").with_command("-js").with_exposed_ports(4222)
    with container:
        wait_for_logs(container, "Server is ready")
        yield f"nats://{container.get_container_host_ip()}:{container.get_exposed_port(4222)}"


def test_health_and_status(nats_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NATS_URL", nats_url)
    monkeypatch.setenv("DEFAULT_TENANT_ID", "local")
    with TestClient(create_app()) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["service"] == "messaging"

        status = client.get("/status").json()
        assert status["provider"] == "loopback"
        assert status["connected"] is True
        assert status["inbound_subject"] == MESSAGING_INBOUND
        assert status["delivered"] == 0


def test_inject_publishes_inbound(nats_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /loopback/inject → an InboundMessage lands on messaging.inbound."""
    monkeypatch.setenv("NATS_URL", nats_url)
    monkeypatch.setenv("DEFAULT_TENANT_ID", "local")

    async def _collect_one() -> InboundMessage:
        got: asyncio.Queue[InboundMessage] = asyncio.Queue()

        async def _on(event: object) -> None:
            await got.put(InboundMessage.model_validate(event.json()))  # type: ignore[attr-defined]

        async with EventBus(nats_url) as bus:
            await bus.subscribe(MESSAGING_INBOUND, _on, tenant_id="local")
            await bus.client.flush()
            with TestClient(create_app()) as client:
                resp = client.post(
                    "/loopback/inject", json={"text": "hello", "channel_id": "room9"}
                )
                assert resp.status_code == 200
                assert resp.json()["session_id"] == "loopback:room9"
                return await asyncio.wait_for(got.get(), timeout=10)

    inbound = asyncio.run(_collect_one())
    assert inbound.text == "hello"
    assert inbound.channel_id == "room9"
    assert inbound.bridge == "loopback"


def test_outbound_is_delivered(nats_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A messaging.outbound reply → the bridge delivers it (delivered count rises)."""
    monkeypatch.setenv("NATS_URL", nats_url)
    monkeypatch.setenv("DEFAULT_TENANT_ID", "local")

    async def _publish_outbound() -> None:
        async with EventBus(nats_url) as bus:
            await bus.publish(
                MESSAGING_OUTBOUND,
                OutboundMessage(
                    tenant="local", bridge="loopback", channel_id="room9", text="hi"
                ).model_dump(),
                tenant_id="local",
            )
            await bus.client.flush()

    with TestClient(create_app()) as client:
        assert client.get("/status").json()["delivered"] == 0
        asyncio.run(_publish_outbound())
        # The subscription delivers asynchronously; poll /status briefly until it lands.
        for _ in range(50):
            if client.get("/status").json()["delivered"] >= 1:
                break
            import time

            time.sleep(0.1)
        assert client.get("/status").json()["delivered"] == 1
