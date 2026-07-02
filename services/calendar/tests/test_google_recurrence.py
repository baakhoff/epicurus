"""Tests for GoogleCalendarProvider's recurrence + attendees mapping (#432).

Google does the actual RRULE expansion server-side (``events.list(singleEvents=true)``,
unchanged since before this feature); the provider only needs to pass ``recurrence``/
``attendees`` through on writes and map the extra read-side fields
(``recurringEventId``, ``originalStartTime``, ``attendees``) onto the domain ``Event``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from epicurus_calendar.models import Attendee
from epicurus_calendar.providers.google import GoogleCalendarProvider, _google_item_to_event


class _StubPlatform:
    async def get_oauth_token(self, provider: str) -> str:
        return "tok"


def _resp(body: dict[str, Any], *, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = MagicMock(return_value=body)
    resp.raise_for_status = MagicMock()
    return resp


def _client_cm(*responses: MagicMock) -> tuple[MagicMock, MagicMock]:
    """A stand-in for ``httpx.AsyncClient(...)`` returning each response in sequence."""
    client = MagicMock()
    seq = list(responses)
    client.get = AsyncMock(side_effect=seq if len(seq) > 1 else lambda *a, **kw: seq[0])
    client.post = AsyncMock(return_value=seq[0])
    client.patch = AsyncMock(return_value=seq[-1])
    client.delete = AsyncMock(return_value=seq[-1])
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm, client


# ── _google_item_to_event: recurrence / attendees / originalStartTime ──────────


def test_maps_recurrence_rrule_line() -> None:
    item = {
        "id": "s1",
        "summary": "Standup",
        "start": {"dateTime": "2026-07-06T09:00:00+00:00"},
        "end": {"dateTime": "2026-07-06T09:30:00+00:00"},
        "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=4"],
    }
    event = _google_item_to_event(item)
    assert event.recurrence == "FREQ=WEEKLY;COUNT=4"


def test_maps_recurrence_ignoring_exdate_lines() -> None:
    # Google can mix RRULE/EXDATE/RDATE in one list; only the RRULE line is mapped — the
    # EXDATE exclusion is already applied server-side to what events.list() returns.
    item = {
        "id": "s1",
        "summary": "Standup",
        "start": {"dateTime": "2026-07-06T09:00:00+00:00"},
        "end": {"dateTime": "2026-07-06T09:30:00+00:00"},
        "recurrence": ["EXDATE:20260713T090000Z", "RRULE:FREQ=WEEKLY;COUNT=4"],
    }
    event = _google_item_to_event(item)
    assert event.recurrence == "FREQ=WEEKLY;COUNT=4"


def test_no_recurrence_field_means_none() -> None:
    item = {
        "id": "e1",
        "summary": "One-off",
        "start": {"dateTime": "2026-07-06T09:00:00+00:00"},
        "end": {"dateTime": "2026-07-06T09:30:00+00:00"},
    }
    event = _google_item_to_event(item)
    assert event.recurrence is None


def test_maps_recurring_event_id_and_original_start() -> None:
    item = {
        "id": "s1_20260713T090000Z",
        "summary": "Standup",
        "start": {"dateTime": "2026-07-13T10:00:00+00:00"},  # moved an hour late
        "end": {"dateTime": "2026-07-13T10:30:00+00:00"},
        "recurringEventId": "s1",
        "originalStartTime": {"dateTime": "2026-07-13T09:00:00+00:00"},
    }
    event = _google_item_to_event(item)
    assert event.recurring_event_id == "s1"
    assert event.original_start is not None
    assert event.original_start.hour == 9  # the original slot, not the moved time
    assert event.start.hour == 10  # the (moved) actual start


def test_maps_attendees() -> None:
    item = {
        "id": "e1",
        "summary": "Sync",
        "start": {"dateTime": "2026-07-06T09:00:00+00:00"},
        "end": {"dateTime": "2026-07-06T09:30:00+00:00"},
        "attendees": [
            {"email": "alice@example.com", "responseStatus": "accepted"},
            {"email": "bob@example.com", "displayName": "Bob", "responseStatus": "needsAction"},
            {"resource": True},  # a room resource with no email — must not crash or appear
        ],
    }
    event = _google_item_to_event(item)
    assert [a.email for a in event.attendees] == ["alice@example.com", "bob@example.com"]
    assert event.attendees[0].response_status == "accepted"
    assert event.attendees[1].display_name == "Bob"


def test_no_attendees_field_means_empty_list() -> None:
    item = {
        "id": "e1",
        "summary": "Solo",
        "start": {"dateTime": "2026-07-06T09:00:00+00:00"},
        "end": {"dateTime": "2026-07-06T09:30:00+00:00"},
    }
    assert _google_item_to_event(item).attendees == []


# ── create_event: recurrence + attendees in the POST body ──────────────────────


async def test_create_event_sends_recurrence_and_attendees() -> None:
    from datetime import UTC, datetime

    prov = GoogleCalendarProvider(platform=_StubPlatform())  # type: ignore[arg-type]
    resp = _resp(
        {
            "id": "s1",
            "summary": "Standup",
            "start": {"dateTime": "2026-07-06T09:00:00+00:00"},
            "end": {"dateTime": "2026-07-06T09:30:00+00:00"},
        }
    )
    cm, client = _client_cm(resp)
    with patch("epicurus_calendar.providers.google.httpx.AsyncClient", return_value=cm):
        await prov.create_event(
            tenant_id="t1",
            title="Standup",
            start=datetime(2026, 7, 6, 9, 0, tzinfo=UTC),
            end=datetime(2026, 7, 6, 9, 30, tzinfo=UTC),
            recurrence="FREQ=WEEKLY;COUNT=4",
            attendees=[Attendee(email="alice@example.com")],
        )
    body = client.post.call_args.kwargs["json"]
    assert body["recurrence"] == ["RRULE:FREQ=WEEKLY;COUNT=4"]
    assert body["attendees"] == [{"email": "alice@example.com"}]


async def test_create_event_omits_recurrence_and_attendees_when_absent() -> None:
    from datetime import UTC, datetime

    prov = GoogleCalendarProvider(platform=_StubPlatform())  # type: ignore[arg-type]
    resp = _resp(
        {
            "id": "e1",
            "summary": "One-off",
            "start": {"dateTime": "2026-07-06T09:00:00+00:00"},
            "end": {"dateTime": "2026-07-06T09:30:00+00:00"},
        }
    )
    cm, client = _client_cm(resp)
    with patch("epicurus_calendar.providers.google.httpx.AsyncClient", return_value=cm):
        await prov.create_event(
            tenant_id="t1",
            title="One-off",
            start=datetime(2026, 7, 6, 9, 0, tzinfo=UTC),
            end=datetime(2026, 7, 6, 9, 30, tzinfo=UTC),
        )
    body = client.post.call_args.kwargs["json"]
    assert "recurrence" not in body
    assert "attendees" not in body


# ── update_event / delete_event edit_scope resolution ───────────────────────────


async def test_update_this_patches_the_given_id_directly() -> None:
    # Google turns a PATCH on an instance id into a per-occurrence exception natively —
    # no extra lookup needed for edit_scope="this".
    prov = GoogleCalendarProvider(platform=_StubPlatform())  # type: ignore[arg-type]
    resp = _resp(
        {
            "id": "s1_20260713T090000Z",
            "summary": "Renamed",
            "start": {"dateTime": "2026-07-13T09:00:00+00:00"},
            "end": {"dateTime": "2026-07-13T09:30:00+00:00"},
        }
    )
    cm, client = _client_cm(resp)
    with patch("epicurus_calendar.providers.google.httpx.AsyncClient", return_value=cm):
        await prov.update_event(
            tenant_id="t1", event_id="s1_20260713T090000Z", title="Renamed", edit_scope="this"
        )
    url = client.patch.call_args.args[0]
    assert url.endswith("/events/s1_20260713T090000Z")


async def test_update_all_resolves_an_instance_id_to_its_series() -> None:
    prov = GoogleCalendarProvider(platform=_StubPlatform())  # type: ignore[arg-type]
    lookup_resp = _resp(
        {
            "id": "s1_20260713T090000Z",
            "summary": "Standup",
            "start": {"dateTime": "2026-07-13T09:00:00+00:00"},
            "end": {"dateTime": "2026-07-13T09:30:00+00:00"},
            "recurringEventId": "s1",
        }
    )
    patch_resp = _resp(
        {
            "id": "s1",
            "summary": "Renamed",
            "start": {"dateTime": "2026-07-06T09:00:00+00:00"},
            "end": {"dateTime": "2026-07-06T09:30:00+00:00"},
        }
    )
    cm, client = _client_cm(lookup_resp, patch_resp)
    with patch("epicurus_calendar.providers.google.httpx.AsyncClient", return_value=cm):
        await prov.update_event(
            tenant_id="t1", event_id="s1_20260713T090000Z", title="Renamed", edit_scope="all"
        )
    url = client.patch.call_args.args[0]
    assert url.endswith("/events/s1")  # patched the series, not the instance


async def test_update_all_given_the_series_id_directly_skips_resolution() -> None:
    prov = GoogleCalendarProvider(platform=_StubPlatform())  # type: ignore[arg-type]
    resp = _resp(
        {
            "id": "s1",
            "summary": "Renamed",
            "start": {"dateTime": "2026-07-06T09:00:00+00:00"},
            "end": {"dateTime": "2026-07-06T09:30:00+00:00"},
        }
    )
    cm, client = _client_cm(resp)
    with patch("epicurus_calendar.providers.google.httpx.AsyncClient", return_value=cm):
        await prov.update_event(tenant_id="t1", event_id="s1", title="Renamed", edit_scope="all")
    url = client.patch.call_args.args[0]
    assert url.endswith("/events/s1")


async def test_update_all_resolution_falls_back_when_lookup_fails() -> None:
    # A GET failure during the series-id resolution must not block the edit — patch the
    # id as given rather than raising.
    prov = GoogleCalendarProvider(platform=_StubPlatform())  # type: ignore[arg-type]
    error_resp = _resp({}, status_code=500)
    error_resp.raise_for_status = MagicMock(side_effect=RuntimeError("boom"))
    patch_resp = _resp(
        {
            "id": "e1",
            "summary": "Renamed",
            "start": {"dateTime": "2026-07-06T09:00:00+00:00"},
            "end": {"dateTime": "2026-07-06T09:30:00+00:00"},
        }
    )
    cm, client = _client_cm(error_resp, patch_resp)
    with patch("epicurus_calendar.providers.google.httpx.AsyncClient", return_value=cm):
        result = await prov.update_event(
            tenant_id="t1", event_id="e1", title="Renamed", edit_scope="all"
        )
    assert result is not None
    url = client.patch.call_args.args[0]
    assert url.endswith("/events/e1")  # fell back to the given id


async def test_delete_this_deletes_the_given_id_directly() -> None:
    prov = GoogleCalendarProvider(platform=_StubPlatform())  # type: ignore[arg-type]
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    cm, client = _client_cm(resp)
    with patch("epicurus_calendar.providers.google.httpx.AsyncClient", return_value=cm):
        assert await prov.delete_event(
            tenant_id="t1", event_id="s1_20260713T090000Z", edit_scope="this"
        )
    url = client.delete.call_args.args[0]
    assert url.endswith("/events/s1_20260713T090000Z")


async def test_delete_all_resolves_an_instance_id_to_its_series() -> None:
    prov = GoogleCalendarProvider(platform=_StubPlatform())  # type: ignore[arg-type]
    lookup_resp = _resp(
        {
            "id": "s1_20260713T090000Z",
            "summary": "Standup",
            "start": {"dateTime": "2026-07-13T09:00:00+00:00"},
            "end": {"dateTime": "2026-07-13T09:30:00+00:00"},
            "recurringEventId": "s1",
        }
    )
    delete_resp = MagicMock()
    delete_resp.status_code = 200
    delete_resp.raise_for_status = MagicMock()
    cm, client = _client_cm(lookup_resp, delete_resp)
    with patch("epicurus_calendar.providers.google.httpx.AsyncClient", return_value=cm):
        assert await prov.delete_event(
            tenant_id="t1", event_id="s1_20260713T090000Z", edit_scope="all"
        )
    url = client.delete.call_args.args[0]
    assert url.endswith("/events/s1")
