"""The core runtime app exposes its ops + platform-API routes and builds cleanly."""

from __future__ import annotations

from fastapi.testclient import TestClient

from epicurus_core import CONTRACT_VERSION
from epicurus_core_app.app import create_app


def test_exposes_ops_and_platform_routes() -> None:
    paths = [getattr(route, "path", "") for route in create_app().routes]
    assert "/health" in paths
    assert "/metrics" in paths
    assert "/platform/v1/info" in paths


def test_platform_info_reports_contract_and_tenant() -> None:
    # No NATS needed: without a `with` block the lifespan (bus.connect) never runs,
    # and these routes don't touch the bus.
    resp = TestClient(create_app()).get("/platform/v1/info")
    assert resp.status_code == 200
    body = resp.json()
    assert body["contract_version"] == CONTRACT_VERSION
    assert body["tenant"] == "local"


def test_health_reports_the_core_service() -> None:
    body = TestClient(create_app()).get("/health").json()
    assert body["service"] == "core-app"
    assert body["version"]
