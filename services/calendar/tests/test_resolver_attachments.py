"""HTTP tests for the entity-ref resolver and chat-attachment source (ADR-0019).

These exercise the routes the core proxies — ``GET /resolve/{kind}/{ref_id}``,
``GET /attachments``, and ``GET /attachments/{ref_id}`` — through a ``TestClient``
with a fake provider, so the wiring (and its 404s) is covered without a DB.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from epicurus_calendar.models import DateTimeRange, Event
from epicurus_calendar.providers.base import CalendarProvider
from epicurus_core import Collection, CollectionPrefs


class _FakeProvider(CalendarProvider):
    """In-memory provider holding a fixed set of events for endpoint tests."""

    name = "local"

    def __init__(self, events: list[Event]) -> None:
        self._events = {e.id: e for e in events}

    async def list_events(
        self, *, tenant_id: str, time_range: DateTimeRange, calendar_id: str | None = None
    ) -> list[Event]:
        return list(self._events.values())

    async def get_event(
        self, *, tenant_id: str, event_id: str, calendar_id: str | None = None
    ) -> Event | None:
        return self._events.get(event_id)

    async def create_event(
        self,
        *,
        tenant_id: str,
        title: str,
        start: datetime,
        end: datetime,
        description: str | None = None,
        location: str | None = None,
        calendar_id: str | None = None,
        all_day: bool = False,
    ) -> Event:  # pragma: no cover - not exercised by these tests
        raise NotImplementedError

    async def update_event(
        self,
        *,
        tenant_id: str,
        event_id: str,
        title: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        description: str | None = None,
        location: str | None = None,
        calendar_id: str | None = None,
        all_day: bool | None = None,
    ) -> Event | None:  # pragma: no cover - not exercised by these tests
        return self._events.get(event_id)

    async def delete_event(
        self, *, tenant_id: str, event_id: str, calendar_id: str | None = None
    ) -> bool:  # pragma: no cover - not exercised by these tests
        return self._events.pop(event_id, None) is not None

    async def find_free_slots(
        self,
        *,
        tenant_id: str,
        time_range: DateTimeRange,
        duration_minutes: int,
        calendar_id: str | None = None,
    ) -> list[DateTimeRange]:  # pragma: no cover - not exercised by these tests
        return []

    async def is_available(self, *, tenant_id: str) -> bool:
        return True

    async def list_collections(self, *, tenant_id: str) -> list[Collection]:
        return []


def _sample_event() -> Event:
    start = datetime(2026, 6, 15, 9, 0, tzinfo=UTC)
    return Event(
        id="e1",
        title="Standup",
        start=start,
        end=start + timedelta(minutes=30),
        description="Daily sync",
        location="Room 4",
        provider="local",
    )


@pytest.fixture()
def client() -> TestClient:
    fake = _FakeProvider([_sample_event()])
    with (
        patch("epicurus_calendar.app.LocalCalendarProvider", return_value=fake),
        patch("epicurus_calendar.app.EventBus.from_settings") as mock_bus_factory,
        # No active selection → the router reads the local fake, with no core round-trip.
        patch(
            "epicurus_calendar.providers.router.CollectionRouter._load_prefs",
            new=AsyncMock(return_value=CollectionPrefs()),
        ),
    ):
        mock_bus_factory.return_value = AsyncMock()
        from epicurus_calendar.app import create_app

        app = create_app()
    return TestClient(app, raise_server_exceptions=True)


class TestResolveEntity:
    def test_event_returns_hovercard(self, client: TestClient) -> None:
        resp = client.get("/resolve/event/e1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["title"] == "Standup"
        assert body["description"] == "Daily sync"
        details = {d["label"]: d["value"] for d in body["details"]}
        assert "When" in details
        assert details["Location"] == "Room 4"
        assert details["Calendar"] == "local"

    def test_unknown_kind_is_404(self, client: TestClient) -> None:
        resp = client.get("/resolve/task/e1")
        assert resp.status_code == 404

    def test_missing_event_is_404(self, client: TestClient) -> None:
        resp = client.get("/resolve/event/nope")
        assert resp.status_code == 404


class TestAttachments:
    def test_picker_lists_events(self, client: TestClient) -> None:
        resp = client.get("/attachments")
        assert resp.status_code == 200
        items = resp.json()
        assert items == [{"ref_id": "e1", "kind": "event", "title": "Standup"}]

    def test_resolve_returns_title_and_excerpt(self, client: TestClient) -> None:
        resp = client.get("/attachments/e1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["title"] == "Standup"
        assert "Daily sync" in body["excerpt"]
        assert "Room 4" in body["excerpt"]

    def test_resolve_missing_is_404(self, client: TestClient) -> None:
        resp = client.get("/attachments/nope")
        assert resp.status_code == 404
