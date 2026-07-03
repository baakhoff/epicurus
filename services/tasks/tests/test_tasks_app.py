"""Tests for the tasks ASGI app (health, status, manifest, page endpoints)."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from epicurus_core import EventBus
from epicurus_tasks.db import TaskStore
from epicurus_tasks.google_provider import GoogleTasksProvider


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


def test_status(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # /status reports the live Google connection (ADR-0030); stub it so the unit test
    # makes no network call to the core.
    monkeypatch.setattr(GoogleTasksProvider, "is_available", AsyncMock(return_value=False))
    resp = client.get("/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["google_connected"] is False


def test_manifest(client: TestClient) -> None:
    resp = client.get("/manifest")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "tasks"
    assert data["version"] == "0.12.0"
    tools = {t["name"] for t in data["tools"]}
    assert tools == {
        "tasks_list",
        "tasks_lists",
        "tasks_add",
        "tasks_complete",
        "tasks_update",
        "tasks_delete",
    }
    # Tasks references tasks (resolver) and is a chat-attachment source (ADR-0019).
    assert data["resolver"] is True
    assert data["attachable"] is True
    # Account/collection model (ADR-0030/0036): multi — each enabled list is a category.
    assert data["collections"]["noun"] == "list"
    assert data["collections"]["multi"] is True
    assert data["collections"]["providers"] == ["google"]
    assert data["ui"]["config_schema"] is None


def test_app_exposes_accounts_route(client: TestClient) -> None:
    """The connected-accounts source the core proxies for the picker (ADR-0030)."""
    from epicurus_core import route_paths

    assert "/accounts" in route_paths(client.app)  # type: ignore[arg-type]


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


def test_page_board_serves_board_data(
    booted_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /pages/board returns the board payload the shell renders (empty store).

    No Google connected → the local store backs the board, the columns are empty, and the
    Add action carries no list picker (ADR-0036). Stub the prefs read so the page makes no
    network call to the core.
    """
    from epicurus_core import CollectionPrefs, PlatformClient

    monkeypatch.setattr(
        PlatformClient, "get_collections", AsyncMock(return_value=CollectionPrefs())
    )
    resp = booted_client.get("/pages/board")
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Tasks"
    assert data["columns"] == []  # fresh in-memory store has no tasks
    add = data["actions"][0]
    assert add["tool"] == "tasks_add"
    assert "list_id" not in add.get("fields", [])  # no list picker without enabled lists


def test_page_board_declares_view_controls(
    booted_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The board declares Group-by + Show controls the shell renders (ADR-0049)."""
    from epicurus_core import CollectionPrefs, PlatformClient

    monkeypatch.setattr(
        PlatformClient, "get_collections", AsyncMock(return_value=CollectionPrefs())
    )
    data = booted_client.get("/pages/board").json()
    controls = {c["id"]: c for c in data["controls"]}
    assert set(controls) == {"group", "show"}
    assert controls["group"]["value"] == "due"  # default grouping
    assert controls["show"]["value"] == "open"  # default filter
    # No enabled lists → the "List" grouping option is omitted.
    assert "list" not in [o["value"] for o in controls["group"]["options"]]


def test_page_board_forwards_and_clamps_query_params(
    booted_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core forwards ?group/?show verbatim; the module echoes valid ones and clamps junk."""
    from epicurus_core import CollectionPrefs, PlatformClient

    monkeypatch.setattr(
        PlatformClient, "get_collections", AsyncMock(return_value=CollectionPrefs())
    )
    valid = booted_client.get("/pages/board?group=priority&show=all").json()
    valid_controls = {c["id"]: c["value"] for c in valid["controls"]}
    assert valid_controls == {"group": "priority", "show": "all"}

    junk = booted_client.get("/pages/board?group=nonsense&show=bogus").json()
    junk_controls = {c["id"]: c["value"] for c in junk["controls"]}
    assert junk_controls == {"group": "due", "show": "open"}  # clamped to defaults
