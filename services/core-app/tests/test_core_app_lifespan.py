"""Integration: the core boots — it migrates its schema, connects to NATS, and serves health.

Requires Docker (for NATS). The database is SQLite under ``tmp_path`` rather than the
``DATABASE_URL`` default's Postgres, because there is no Postgres on this runner and what the
test is for is that the *lifespan* works end to end. Since #927 that includes ``run_migrations``
— the one call that now builds all 40 of the core's tables, replacing 29 per-store ``init()``
calls, and deliberately **not** wrapped in try/except: a database the core cannot reach has to
fail the boot rather than degrade 29 features silently. The real-Postgres upgrade paths belong to
the `migrations` CI gate.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

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


def test_core_boots_and_serves_health(
    nats_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NATS_URL", nats_url)
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'core.db'}")
    # __enter__ runs the lifespan: upgrade head, then bus.connect.
    with TestClient(create_app()) as client:
        assert client.get("/health").json()["service"] == "core-app"
        assert client.get("/platform/v1/info").json()["tenant"] == "local"
