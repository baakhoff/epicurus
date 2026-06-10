"""Integration: the core boots (its lifespan connects to NATS) and serves health.

Requires Docker.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

from epicurus_core_app.app import create_app

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def nats_url() -> Iterator[str]:
    container = DockerContainer("nats:2.10").with_command("-js").with_exposed_ports(4222)
    with container:
        wait_for_logs(container, "Server is ready")
        yield f"nats://{container.get_container_host_ip()}:{container.get_exposed_port(4222)}"


def test_core_boots_and_serves_health(nats_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NATS_URL", nats_url)
    with TestClient(create_app()) as client:  # __enter__ runs the lifespan (bus.connect)
        assert client.get("/health").json()["service"] == "core-app"
        assert client.get("/platform/v1/info").json()["tenant"] == "local"
