"""Tests for the Google backend's incremental-sync seam (#831).

Everything here is a stubbed ``httpx.AsyncClient`` — there is **no live Google anywhere**. What
is being pinned is the translation both ways: the query Google actually needs for a usable sync
token, and the mapping from its ``events.list`` items to neutral :class:`EventChange`s, tombstones
included.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from epicurus_calendar.providers.google import GoogleCalendarProvider

TENANT = "local"


class _StubPlatform:
    async def get_oauth_token(self, provider: str) -> str:
        return "tok"


class _FakeHttp:
    """Serves a scripted list of ``events.list`` responses, recording every request's params."""

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = list(pages)
        self.requests: list[dict[str, Any]] = []
        self.urls: list[str] = []

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None, params: dict[str, Any]
    ) -> MagicMock:
        self.urls.append(url)
        self.requests.append(dict(params))
        body = self._pages.pop(0) if self._pages else {"items": []}
        resp = MagicMock()
        resp.json = MagicMock(return_value=body)
        resp.raise_for_status = MagicMock()
        return resp


def _client_cm(http: object) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=http)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _provider() -> GoogleCalendarProvider:
    return GoogleCalendarProvider(platform=_StubPlatform())  # type: ignore[arg-type]


def _item(event_id: str, *, hour: int = 9, **extra: Any) -> dict[str, Any]:
    day = datetime(2026, 6, 15, tzinfo=UTC)
    return {
        "id": event_id,
        "summary": "Standup",
        "status": "confirmed",
        "start": {"dateTime": day.replace(hour=hour).isoformat()},
        "end": {"dateTime": day.replace(hour=hour + 1).isoformat()},
        **extra,
    }


def test_google_declares_itself_a_sync_target() -> None:
    """The flag the router filters reconcile targets on."""
    assert _provider().supports_sync is True


async def test_full_sync_asks_for_the_query_a_sync_token_requires() -> None:
    """``showDeleted`` is what makes a later delta able to report a cancellation at all, and
    ``singleEvents`` must match what every other read path sees. Google binds both to the token
    it mints, so getting them wrong here is unrecoverable without a resync."""
    http = _FakeHttp([{"items": [_item("e1")], "nextSyncToken": "tok-1"}])
    since = datetime(2026, 5, 16, tzinfo=UTC)
    with patch(
        "epicurus_calendar.providers.google.httpx.AsyncClient", return_value=_client_cm(http)
    ):
        page = await _provider().full_sync(tenant_id=TENANT, calendar_id="work@group", since=since)
    [params] = http.requests
    assert params["singleEvents"] == "true"
    assert params["showDeleted"] == "true"
    assert params["timeMin"].startswith("2026-05-16T00:00:00")
    assert "work@group" in http.urls[0]
    assert page.next_cursor == "tok-1"
    assert [c.event_id for c in page.changes] == ["e1"]


async def test_full_sync_falls_back_to_the_configured_calendar() -> None:
    http = _FakeHttp([{"items": [], "nextSyncToken": "tok-1"}])
    with patch(
        "epicurus_calendar.providers.google.httpx.AsyncClient", return_value=_client_cm(http)
    ):
        await _provider().full_sync(tenant_id=TENANT, since=datetime.now(UTC))
    assert "/calendars/primary/events" in http.urls[0]


async def test_an_incremental_call_sends_only_the_token() -> None:
    """Google rejects a request that changes the query mid-sync; everything else is inherited."""
    http = _FakeHttp([{"items": [], "nextSyncToken": "tok-2"}])
    with patch(
        "epicurus_calendar.providers.google.httpx.AsyncClient", return_value=_client_cm(http)
    ):
        page = await _provider().changed_events_since(tenant_id=TENANT, cursor="tok-1")
    [params] = http.requests
    assert params["syncToken"] == "tok-1"
    assert "timeMin" not in params
    assert "singleEvents" not in params
    assert page is not None and page.next_cursor == "tok-2"


async def test_pagination_walks_until_the_token_appears() -> None:
    """The sync token only rides on the **final** page, so a partial walk has no cursor."""
    http = _FakeHttp(
        [
            {"items": [_item("e1")], "nextPageToken": "p2"},
            {"items": [_item("e2")], "nextPageToken": "p3"},
            {"items": [_item("e3")], "nextSyncToken": "tok-9"},
        ]
    )
    with patch(
        "epicurus_calendar.providers.google.httpx.AsyncClient", return_value=_client_cm(http)
    ):
        page = await _provider().changed_events_since(tenant_id=TENANT, cursor="tok-1")
    assert page is not None
    assert [c.event_id for c in page.changes] == ["e1", "e2", "e3"]
    assert page.next_cursor == "tok-9"
    assert [r.get("pageToken") for r in http.requests] == [None, "p2", "p3"]


async def test_a_walk_that_never_terminates_yields_no_cursor() -> None:
    """The page budget. Returning a token here would skip whatever the unread pages held, so it
    returns none and the next pass full-syncs and diffs instead."""
    endless = [
        {"items": [_item(f"e{i}")], "nextPageToken": f"p{i}", "nextSyncToken": "premature"}
        for i in range(200)
    ]
    http = _FakeHttp(endless)
    with patch(
        "epicurus_calendar.providers.google.httpx.AsyncClient", return_value=_client_cm(http)
    ):
        page = await _provider().changed_events_since(tenant_id=TENANT, cursor="tok-1")
    assert page is not None
    assert page.next_cursor is None
    assert len(http.requests) == 40


async def test_an_expired_token_reports_gone_rather_than_raising() -> None:
    """``410 GONE`` is the "start over" signal (Gmail's expired ``historyId``, one module over).
    It must be an ordinary control-flow outcome, not an exception the loop has to catch."""

    class _Gone:
        async def get(self, url: str, **kwargs: Any) -> MagicMock:
            request = httpx.Request("GET", url)
            response = httpx.Response(410, request=request)
            raise httpx.HTTPStatusError("gone", request=request, response=response)

    with patch(
        "epicurus_calendar.providers.google.httpx.AsyncClient",
        return_value=_client_cm(_Gone()),
    ):
        assert await _provider().changed_events_since(tenant_id=TENANT, cursor="stale") is None


async def test_any_other_http_error_still_propagates() -> None:
    """Only ``410`` means "start over". A 403 is a real failure and must reach the loop's
    error handling rather than be mistaken for a lapsed cursor."""

    class _Forbidden:
        async def get(self, url: str, **kwargs: Any) -> MagicMock:
            request = httpx.Request("GET", url)
            response = httpx.Response(403, request=request)
            raise httpx.HTTPStatusError("forbidden", request=request, response=response)

    with (
        patch(
            "epicurus_calendar.providers.google.httpx.AsyncClient",
            return_value=_client_cm(_Forbidden()),
        ),
        pytest.raises(httpx.HTTPStatusError),
    ):
        await _provider().changed_events_since(tenant_id=TENANT, cursor="tok-1")


async def test_a_cancelled_item_becomes_a_bare_tombstone() -> None:
    """Google strips a cancelled item to little more than an id — no summary, no start. Parsing
    it as an event would blow up; carrying ``series_id`` on the change is what keeps a cancelled
    occurrence attributable to its series."""
    http = _FakeHttp(
        [
            {
                "items": [
                    {"id": "s1_20260615T090000Z", "status": "cancelled", "recurringEventId": "s1"}
                ],
                "nextSyncToken": "tok-2",
            }
        ]
    )
    with patch(
        "epicurus_calendar.providers.google.httpx.AsyncClient", return_value=_client_cm(http)
    ):
        page = await _provider().changed_events_since(tenant_id=TENANT, cursor="tok-1")
    assert page is not None
    [change] = page.changes
    assert change.cancelled is True
    assert change.event is None
    assert change.series_id == "s1"


async def test_a_live_item_maps_through_the_same_parser_every_read_uses() -> None:
    """Same parser means the change hash the reconcile computes matches the one the write seam
    computed for the identical event — the whole basis of update detection."""
    http = _FakeHttp(
        [
            {
                "items": [
                    _item(
                        "s1_20260615T090000Z",
                        recurringEventId="s1",
                        location="Room 2",
                        description="agenda",
                    )
                ],
                "nextSyncToken": "tok-2",
            }
        ]
    )
    with patch(
        "epicurus_calendar.providers.google.httpx.AsyncClient", return_value=_client_cm(http)
    ):
        page = await _provider().changed_events_since(tenant_id=TENANT, cursor="tok-1")
    assert page is not None
    [change] = page.changes
    assert change.cancelled is False
    assert change.series_id == "s1"
    assert change.event is not None
    assert change.event.title == "Standup"
    assert change.event.provider == "google"
    assert change.event.location == "Room 2"
    assert change.event.recurring_event_id == "s1"


async def test_an_all_day_item_keeps_its_date_only_shape() -> None:
    http = _FakeHttp(
        [
            {
                "items": [
                    {
                        "id": "e-all-day",
                        "summary": "Conference",
                        "status": "confirmed",
                        "start": {"date": "2026-06-15"},
                        "end": {"date": "2026-06-16"},
                    }
                ],
                "nextSyncToken": "tok-2",
            }
        ]
    )
    with patch(
        "epicurus_calendar.providers.google.httpx.AsyncClient", return_value=_client_cm(http)
    ):
        page = await _provider().changed_events_since(tenant_id=TENANT, cursor="tok-1")
    assert page is not None
    [change] = page.changes
    assert change.event is not None
    assert change.event.all_day is True
    assert change.event.end - change.event.start == timedelta(days=1)


async def test_a_malformed_item_is_dropped_not_fatal() -> None:
    """One unparseable row must not cost a whole sync pass — the healthy siblings still land."""
    http = _FakeHttp(
        [
            {
                "items": [
                    {"summary": "no id at all", "status": "confirmed"},
                    {"id": "broken", "status": "confirmed", "start": {}, "end": {}},
                    _item("good"),
                ],
                "nextSyncToken": "tok-2",
            }
        ]
    )
    with patch(
        "epicurus_calendar.providers.google.httpx.AsyncClient", return_value=_client_cm(http)
    ):
        page = await _provider().changed_events_since(tenant_id=TENANT, cursor="tok-1")
    assert page is not None
    assert [c.event_id for c in page.changes] == ["good"]
