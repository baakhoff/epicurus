"""Tests for GoogleCalendarProvider.get_timezone (ADR-0039)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from epicurus_calendar.providers.google import GoogleCalendarProvider


class _StubPlatform:
    async def get_oauth_token(self, provider: str) -> str:
        return "tok"


class _BadPlatform:
    async def get_oauth_token(self, provider: str) -> str:
        raise RuntimeError("not connected")


def _client_cm(value: Any) -> MagicMock:
    """A stand-in for ``httpx.AsyncClient(...)`` returning ``{"value": value}``."""
    resp = MagicMock()
    resp.json = MagicMock(return_value={"value": value})
    resp.raise_for_status = MagicMock()
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


async def test_get_timezone_returns_value() -> None:
    prov = GoogleCalendarProvider(platform=_StubPlatform())  # type: ignore[arg-type]
    with patch(
        "epicurus_calendar.providers.google.httpx.AsyncClient",
        return_value=_client_cm("Europe/Belgrade"),
    ):
        assert await prov.get_timezone(tenant_id="local") == "Europe/Belgrade"


async def test_get_timezone_none_when_token_fails() -> None:
    prov = GoogleCalendarProvider(platform=_BadPlatform())  # type: ignore[arg-type]
    assert await prov.get_timezone(tenant_id="local") is None


async def test_get_timezone_none_when_value_missing() -> None:
    prov = GoogleCalendarProvider(platform=_StubPlatform())  # type: ignore[arg-type]
    with patch(
        "epicurus_calendar.providers.google.httpx.AsyncClient",
        return_value=_client_cm(None),
    ):
        assert await prov.get_timezone(tenant_id="local") is None
