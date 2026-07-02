"""Tests for GoogleCalendarProvider.list_collections (ADR-0030, #433)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from epicurus_calendar.providers.google import GoogleCalendarProvider


class _StubPlatform:
    async def get_oauth_token(self, provider: str) -> str:
        return "tok"


def _client_cm(items: list[dict[str, Any]]) -> MagicMock:
    """A stand-in for ``httpx.AsyncClient(...)`` returning a calendarList payload."""
    resp = MagicMock()
    resp.json = MagicMock(return_value={"items": items})
    resp.raise_for_status = MagicMock()
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


async def test_list_collections_sorts_primary_first() -> None:
    # The calendarList API's order is unspecified; the primary must sort first so it is
    # the natural default wherever "the first Google calendar" is picked (#433).
    items = [
        {"id": "holidays@group", "summary": "Holidays", "accessRole": "reader"},
        {
            "id": "me@example.com",
            "summary": "me@example.com",
            "accessRole": "owner",
            "primary": True,
        },
        {"id": "team@group", "summary": "Team", "accessRole": "writer"},
    ]
    prov = GoogleCalendarProvider(platform=_StubPlatform())  # type: ignore[arg-type]
    with patch(
        "epicurus_calendar.providers.google.httpx.AsyncClient",
        return_value=_client_cm(items),
    ):
        collections = await prov.list_collections(tenant_id="local")
    assert [c.collection for c in collections] == ["me@example.com", "holidays@group", "team@group"]
    assert collections[0].writable is True
    assert collections[1].writable is False  # reader role → not offered as a write target


async def test_list_collections_maps_titles_and_roles() -> None:
    items = [
        {
            "id": "me@example.com",
            "summary": "me@example.com",
            "summaryOverride": "Personal",
            "accessRole": "owner",
            "primary": True,
        },
    ]
    prov = GoogleCalendarProvider(platform=_StubPlatform())  # type: ignore[arg-type]
    with patch(
        "epicurus_calendar.providers.google.httpx.AsyncClient",
        return_value=_client_cm(items),
    ):
        collections = await prov.list_collections(tenant_id="local")
    assert collections[0].title == "Personal"  # the operator's rename wins over the raw summary
    assert collections[0].account == "google"
