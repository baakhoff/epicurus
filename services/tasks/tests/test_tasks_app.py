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


def _build_booted_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Construct (but don't enter) a lifespan-running TestClient over the tasks app.

    Split out from the ``booted_client`` fixture so a test that must patch *before*
    ``create_app`` runs — e.g. stubbing ``PlatformClient.get_timezone`` (captured by the
    operator clock at build time, #555) or ``operator_clock`` itself — can apply its
    patches, then boot. NATS isn't available in unit tests, so the EventBus connect/close
    are stubbed to no-ops.
    """
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("NATS_URL", "nats://localhost:4222")

    async def _noop(self: EventBus) -> None:
        return None

    monkeypatch.setattr(EventBus, "connect", _noop)
    monkeypatch.setattr(EventBus, "close", _noop)

    from epicurus_tasks.app import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture()
def booted_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A TestClient whose lifespan actually runs, so the local store is created.

    The page endpoint queries the DB, which the lifespan's ``store.init()`` builds —
    so unlike ``client`` we enter the app's lifespan.
    """
    with _build_booted_client(monkeypatch) as the_client:
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
    assert data["version"] == "0.16.0"
    tools = {t["name"] for t in data["tools"]}
    assert tools == {
        "tasks_list",
        "tasks_lists",
        "tasks_create_list",
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


# ── calendar-feed endpoint wiring (#469) — filtering logic itself is unit-tested
# exhaustively in test_board.py against `calendar_feed_items` directly; these confirm
# only that the route exists, threads the range through, and degrades cleanly.


def test_calendar_feed_empty_store_returns_empty_list(
    booted_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from epicurus_core import CollectionPrefs, PlatformClient

    monkeypatch.setattr(
        PlatformClient, "get_collections", AsyncMock(return_value=CollectionPrefs())
    )
    resp = booted_client.get("/calendar-feed?start=2026-07-01&end=2026-08-01")
    assert resp.status_code == 200
    assert resp.json() == []


def test_calendar_feed_requires_start_and_end(client: TestClient) -> None:
    """No 500 on a missing range — FastAPI's own required-query-param 422."""
    resp = client.get("/calendar-feed")
    assert resp.status_code == 422


# ── board Today/Overdue grouping uses the operator's day, not UTC's (#555) ──────────
#
# The board's `today` must come from the same operator-timezone clock the router's overdue
# sweep runs on, so the display grouping and the sweep never disagree across the UTC/operator
# midnight (a task the sweep counts as due today must not land in the board's Overdue column).
# `build_tasks_board` itself is already unit-tested deterministically given `today`
# (test_board.py); these assert only that `page()` derives `today` from the operator clock.


def _capture_board_today(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Spy on ``build_tasks_board`` so a page render exposes the ``today`` it grouped by.

    Patched in the app's namespace (where ``page()`` looks it up) before the client boots;
    returns a dict the caller reads ``["today"]`` from after the request. The real board
    payload is still built and returned, so the endpoint behaves normally.
    """
    import epicurus_tasks.app as app_module
    from epicurus_tasks.service import build_tasks_board

    captured: dict[str, str] = {}

    def _spy(tasks: object, **kwargs: object) -> object:
        captured["today"] = str(kwargs["today"])
        return build_tasks_board(tasks, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(app_module, "build_tasks_board", _spy)
    return captured


def test_board_page_groups_by_operator_timezone_not_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    """The board groups by the operator's day: `today` resolves in their zone (#555).

    Straddle proof: a far-ahead zone (Kiritimati, UTC+14) is a day ahead of UTC for much of
    the UTC day. Comparing against ``datetime.now(zone)`` computed here — not a hardcoded UTC
    date — keeps this deterministic without freezing the wall clock, the same technique the
    operator-clock unit test uses. Under the pre-#555 ``datetime.now(UTC)`` the board grouped
    by the UTC day instead.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from epicurus_core import CollectionPrefs, PlatformClient

    zone = "Pacific/Kiritimati"
    # get_timezone is captured by operator_clock at create_app time, so it must be patched
    # before the client boots (a post-boot patch wouldn't reach the already-built clock).
    monkeypatch.setattr(PlatformClient, "get_timezone", AsyncMock(return_value=zone))
    monkeypatch.setattr(
        PlatformClient, "get_collections", AsyncMock(return_value=CollectionPrefs())
    )
    captured = _capture_board_today(monkeypatch)

    with _build_booted_client(monkeypatch) as client:
        resp = client.get("/pages/board")

    assert resp.status_code == 200
    assert captured["today"] == datetime.now(ZoneInfo(zone)).date().isoformat()


def test_board_page_uses_the_operator_clock_output_over_wall_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deterministic regression guard: `page()` groups by the operator clock's output, not
    ``datetime.now(UTC)``. Pinning the clock to a fixed sentinel day (far from today) makes
    the two unmistakably different — the pre-#555 code grouped by the real UTC day."""
    import epicurus_tasks.app as app_module
    from epicurus_core import CollectionPrefs, PlatformClient

    sentinel = "2020-06-15"

    def _fixed_clock(_source: object) -> object:
        async def _today() -> str:
            return sentinel

        return _today

    monkeypatch.setattr(app_module, "operator_clock", _fixed_clock)
    monkeypatch.setattr(
        PlatformClient, "get_collections", AsyncMock(return_value=CollectionPrefs())
    )
    captured = _capture_board_today(monkeypatch)

    with _build_booted_client(monkeypatch) as client:
        resp = client.get("/pages/board")

    assert resp.status_code == 200
    assert captured["today"] == sentinel


def test_board_page_falls_back_to_utc_when_timezone_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core unreachable → the board's `today` degrades to UTC, matching the sweep's own
    fallback (both go through ``operator_clock`` → ``_resolve_timezone``), so the display and
    the sweep still agree on the day (#555)."""
    from datetime import UTC, datetime

    from epicurus_core import CollectionPrefs, PlatformClient

    async def _boom(*args: object, **kwargs: object) -> str:
        raise RuntimeError("core down")

    monkeypatch.setattr(PlatformClient, "get_timezone", _boom)
    monkeypatch.setattr(
        PlatformClient, "get_collections", AsyncMock(return_value=CollectionPrefs())
    )
    captured = _capture_board_today(monkeypatch)

    with _build_booted_client(monkeypatch) as client:
        resp = client.get("/pages/board")

    assert resp.status_code == 200
    assert captured["today"] == datetime.now(UTC).date().isoformat()
