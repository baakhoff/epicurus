"""Tests for GoogleCalendarProvider's Meet-link support (#444).

Attaching a Meet conference on create is Google-only — the local provider has no
conferencing backend to mirror it against (covered separately in test_local_provider.py).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from epicurus_calendar.providers.google import GoogleCalendarProvider, _google_item_to_event


class _StubPlatform:
    async def get_oauth_token(self, provider: str) -> str:
        return "tok"


def _resp(body: dict[str, Any]) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(return_value=body)
    resp.raise_for_status = MagicMock()
    return resp


def _client_cm(resp: MagicMock) -> tuple[MagicMock, MagicMock]:
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm, client


# ── create_event: conferenceData in the request ─────────────────────────────────


async def test_create_event_sends_conference_data_when_add_meet() -> None:
    prov = GoogleCalendarProvider(platform=_StubPlatform())  # type: ignore[arg-type]
    resp = _resp(
        {
            "id": "e1",
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
            add_meet=True,
        )
    body = client.post.call_args.kwargs["json"]
    conference = body["conferenceData"]["createRequest"]
    assert conference["conferenceSolutionKey"] == {"type": "hangoutsMeet"}
    assert isinstance(conference["requestId"], str) and conference["requestId"]
    assert client.post.call_args.kwargs["params"] == {"conferenceDataVersion": 1}


async def test_create_event_omits_conference_data_by_default() -> None:
    prov = GoogleCalendarProvider(platform=_StubPlatform())  # type: ignore[arg-type]
    resp = _resp(
        {
            "id": "e1",
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
        )
    body = client.post.call_args.kwargs["json"]
    assert "conferenceData" not in body
    assert client.post.call_args.kwargs["params"] == {}


async def test_create_event_each_call_gets_a_distinct_request_id() -> None:
    # requestId is Google's idempotency key for the conference-creation sub-request — reusing
    # one across two distinct events would be a bug, not a feature.
    prov = GoogleCalendarProvider(platform=_StubPlatform())  # type: ignore[arg-type]
    resp = _resp(
        {
            "id": "e1",
            "summary": "Standup",
            "start": {"dateTime": "2026-07-06T09:00:00+00:00"},
            "end": {"dateTime": "2026-07-06T09:30:00+00:00"},
        }
    )
    request_ids = []
    for _ in range(2):
        cm, client = _client_cm(resp)
        with patch("epicurus_calendar.providers.google.httpx.AsyncClient", return_value=cm):
            await prov.create_event(
                tenant_id="t1",
                title="Standup",
                start=datetime(2026, 7, 6, 9, 0, tzinfo=UTC),
                end=datetime(2026, 7, 6, 9, 30, tzinfo=UTC),
                add_meet=True,
            )
        request_ids.append(
            client.post.call_args.kwargs["json"]["conferenceData"]["createRequest"]["requestId"]
        )
    assert request_ids[0] != request_ids[1]


# ── _google_item_to_event: mapping conferenceData.entryPoints → meet_url ────────


def test_maps_meet_url_from_the_video_entry_point() -> None:
    item = {
        "id": "e1",
        "summary": "Standup",
        "start": {"dateTime": "2026-07-06T09:00:00+00:00"},
        "end": {"dateTime": "2026-07-06T09:30:00+00:00"},
        "conferenceData": {
            "entryPoints": [
                {"entryPointType": "phone", "uri": "tel:+1-555-0100"},
                {"entryPointType": "video", "uri": "https://meet.google.com/abc-defg-hij"},
                {"entryPointType": "more", "uri": "https://meet.google.com/abc-defg-hij?more"},
            ]
        },
    }
    event = _google_item_to_event(item)
    assert event.meet_url == "https://meet.google.com/abc-defg-hij"


def test_no_conference_data_means_no_meet_url() -> None:
    item = {
        "id": "e1",
        "summary": "One-off",
        "start": {"dateTime": "2026-07-06T09:00:00+00:00"},
        "end": {"dateTime": "2026-07-06T09:30:00+00:00"},
    }
    assert _google_item_to_event(item).meet_url is None


def test_conference_data_with_no_video_entry_point_means_no_meet_url() -> None:
    # A non-Meet conferencing solution (or a still-provisioning one) may have entry points
    # with no "video" type — degrade to None rather than picking the wrong link.
    item = {
        "id": "e1",
        "summary": "Standup",
        "start": {"dateTime": "2026-07-06T09:00:00+00:00"},
        "end": {"dateTime": "2026-07-06T09:30:00+00:00"},
        "conferenceData": {"entryPoints": [{"entryPointType": "phone", "uri": "tel:+1-555-0100"}]},
    }
    assert _google_item_to_event(item).meet_url is None


def test_conference_data_pending_with_no_entry_points_means_no_meet_url() -> None:
    # A conference still being provisioned has no entryPoints yet — best-effort, not polled.
    item = {
        "id": "e1",
        "summary": "Standup",
        "start": {"dateTime": "2026-07-06T09:00:00+00:00"},
        "end": {"dateTime": "2026-07-06T09:30:00+00:00"},
        "conferenceData": {
            "createRequest": {"requestId": "r1", "status": {"statusCode": "pending"}}
        },
    }
    assert _google_item_to_event(item).meet_url is None
