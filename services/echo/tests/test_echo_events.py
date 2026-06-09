"""Integration test: the echo NATS request/reply path. Requires Docker."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

from epicurus_core import EventBus
from epicurus_echo.service import ECHO_SUBJECT, serve_responder

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def nats_url() -> Iterator[str]:
    container = DockerContainer("nats:2.10").with_command("-js").with_exposed_ports(4222)
    with container:
        wait_for_logs(container, "Server is ready")
        yield f"nats://{container.get_container_host_ip()}:{container.get_exposed_port(4222)}"


async def test_echo_request_reply(nats_url: str) -> None:
    async with EventBus(nats_url) as bus:
        await serve_responder(bus, "local")
        await bus.client.flush()
        reply = await bus.request(ECHO_SUBJECT, b"ping", tenant_id="local")
    # The responder echoes the request payload back (the reply arrives on a
    # temporary NATS inbox subject, so we assert on the data, not the subject).
    assert reply.data == b"ping"
