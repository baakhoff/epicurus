"""Tests for the tasks ASGI app (health, status, manifest endpoints)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from epicurus_tasks.db import TaskStore


@pytest.fixture()
async def local_store() -> TaskStore:
    """In-memory SQLite store for app tests."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = TaskStore(engine)
    await store.init()
    return store


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient pointing at the tasks app using the local provider + in-memory SQLite."""
    monkeypatch.setenv("TASKS_PROVIDER", "local")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("NATS_URL", "nats://localhost:4222")

    from epicurus_tasks.app import create_app

    the_app = create_app()
    return TestClient(the_app, raise_server_exceptions=False)


def test_health(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["service"] == "tasks"


def test_status(client: TestClient) -> None:
    resp = client.get("/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["provider"] == "local"


def test_manifest(client: TestClient) -> None:
    resp = client.get("/manifest")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "tasks"
    tools = {t["name"] for t in data["tools"]}
    assert tools == {"tasks_list", "tasks_add", "tasks_complete"}
