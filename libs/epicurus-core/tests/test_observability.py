"""Tests for the /health and /metrics operational surface."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from epicurus_core import __version__
from epicurus_core.observability import add_ops_routes


def _client() -> TestClient:
    app = FastAPI()
    add_ops_routes(app, service_name="test-svc")
    return TestClient(app)


def test_health() -> None:
    resp = _client().get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "test-svc", "version": __version__}


def test_metrics() -> None:
    resp = _client().get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert isinstance(resp.text, str)


def test_health_reports_given_version() -> None:
    app = FastAPI()
    add_ops_routes(app, service_name="svc", version="9.9.9")
    resp = TestClient(app).get("/health")
    assert resp.json() == {"status": "ok", "service": "svc", "version": "9.9.9"}
