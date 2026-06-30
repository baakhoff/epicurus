"""Integration tests: the messaging app's NATS wiring end-to-end. Requires Docker.

Boots the full app (its lifespan connects NATS, subscribes ``messaging.outbound``, and starts
every bridge) and proves both directions over a real NATS container:

* ``POST /loopback/inject`` → the loopback bridge publishes ``messaging.inbound``;
* a published ``messaging.outbound`` → it is dispatched to the matching bridge (visible in the
  loopback bridge's ``detail`` in ``/status``);
* ``POST /bridges/{bridge}/reload`` → the core's runtime connect/disconnect control path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

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


def _bridge(status: dict[str, Any], name: str) -> dict[str, Any]:
    """The one bridge entry named ``name`` from a ``/status`` body."""
    return next(b for b in status["bridges"] if b["bridge"] == name)


def test_health_and_status(nats_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NATS_URL", nats_url)
    monkeypatch.setenv("DEFAULT_TENANT_ID", "local")
    with TestClient(create_app()) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["service"] == "messaging"

        status = client.get("/status").json()
        assert status["inbound_subject"] == MESSAGING_INBOUND
        assert status["outbound_subject"] == MESSAGING_OUTBOUND

        loopback = _bridge(status, "loopback")
        assert loopback["connected"] is True
        assert loopback["manageable"] is False

        # Discord is constructed but dormant (no token configured in this environment).
        discord = _bridge(status, "discord")
        assert discord["manageable"] is True
        assert discord["configured"] is False
        assert discord["connected"] is False


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
    """A messaging.outbound reply for the loopback bridge → it is dispatched and delivered."""
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
        assert "0 delivered" in _bridge(client.get("/status").json(), "loopback")["detail"]
        asyncio.run(_publish_outbound())
        # The subscription delivers asynchronously; poll /status briefly until it lands.
        for _ in range(50):
            if "1 delivered" in _bridge(client.get("/status").json(), "loopback")["detail"]:
                break
            import time

            time.sleep(0.1)
        assert "1 delivered" in _bridge(client.get("/status").json(), "loopback")["detail"]


def test_reload_bridge_control_path(nats_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /bridges/{bridge}/reload reconnects one bridge (404 for an unknown one)."""
    monkeypatch.setenv("NATS_URL", nats_url)
    monkeypatch.setenv("DEFAULT_TENANT_ID", "local")
    with TestClient(create_app()) as client:
        ok = client.post("/bridges/discord/reload")
        assert ok.status_code == 200
        assert ok.json()["bridge"] == "discord"
        assert ok.json()["connected"] is False  # still no token → still dormant

        missing = client.post("/bridges/nope/reload")
        assert missing.status_code == 404
