"""Tests for the tasks ASGI app (health, status, manifest, page endpoints)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from epicurus_core import EventBus
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


@pytest.fixture()
def booted_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A TestClient whose lifespan actually runs, so the local store is created.

    The page endpoint queries the DB, which the lifespan's ``store.init()`` builds —
    so unlike ``client`` we enter the app's lifespan. NATS isn't available in unit
    tests, so the EventBus connect/close are stubbed to no-ops (the existing tests
    skip lifespan for exactly this reason).
    """
    monkeypatch.setenv("TASKS_PROVIDER", "local")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("NATS_URL", "nats://localhost:4222")

    async def _noop(self: EventBus) -> None:
        return None

    monkeypatch.setattr(EventBus, "connect", _noop)
    monkeypatch.setattr(EventBus, "close", _noop)

    from epicurus_tasks.app import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as the_client:
        yield the_client


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
    assert data["version"] == "0.4.0"
    tools = {t["name"] for t in data["tools"]}
    assert tools == {"tasks_list", "tasks_add", "tasks_complete", "tasks_update"}
    # Tasks references tasks (resolver) and is a chat-attachment source (ADR-0019).
    assert data["resolver"] is True
    assert data["attachable"] is True


def test_manifest_declares_tasks_board_page(client: TestClient) -> None:
    """The Tasks left-nav page is declared as a core `board` archetype (ADR-0018)."""
    data = client.get("/manifest").json()
    pages = {p["id"]: p for p in data["pages"]}
    assert "board" in pages
    assert pages["board"]["archetype"] == "board"
    assert pages["board"]["title"] == "Tasks"


def test_page_unknown_id_404s(client: TestClient) -> None:
    """The 404 guard fires before any DB access — no lifespan needed."""
    resp = client.get("/pages/does-not-exist")
    assert resp.status_code == 404


def test_page_board_serves_board_data(booted_client: TestClient) -> None:
    """GET /pages/board returns the board payload the shell renders (empty store)."""
    resp = booted_client.get("/pages/board")
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Tasks"
    assert data["columns"] == []  # fresh in-memory store has no tasks
    assert data["actions"][0]["tool"] == "tasks_add"
