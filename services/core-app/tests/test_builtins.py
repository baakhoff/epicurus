"""Tests for the core built-in `now` tool (ADR-0039)."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from epicurus_core_app.agent.builtins import NOW_SPEC, make_now_handler

Handler = Callable[[dict[str, Any]], Awaitable[str]]


def _handler(
    tz: str = "Europe/Belgrade",
    calendar_tz: str | None = None,
    *,
    calendar_raises: bool = False,
) -> Handler:
    async def tz_provider() -> str:
        return tz

    async def calendar_tz_provider() -> str | None:
        if calendar_raises:
            raise RuntimeError("calendar down")
        return calendar_tz

    return make_now_handler(tz_provider, calendar_tz_provider)


def test_now_spec_shape() -> None:
    assert NOW_SPEC["function"]["name"] == "now"
    assert "timezone" in NOW_SPEC["function"]["parameters"]["properties"]


async def test_now_uses_configured_timezone() -> None:
    out = json.loads(await _handler(tz="Europe/Belgrade")({}))
    assert out["timezone"] == "Europe/Belgrade"
    assert "datetime" in out
    assert "utc" in out
    assert "weekday" in out


async def test_now_timezone_argument_overrides_setting() -> None:
    out = json.loads(await _handler(tz="UTC")({"timezone": "Asia/Tokyo"}))
    assert out["timezone"] == "Asia/Tokyo"


async def test_now_invalid_timezone_falls_back_to_utc() -> None:
    out = json.loads(await _handler(tz="Not/AZone")({}))
    assert out["timezone"] == "UTC"


async def test_now_reports_calendar_timezone_mismatch() -> None:
    out = json.loads(await _handler(tz="Europe/Belgrade", calendar_tz="Asia/Almaty")({}))
    assert out["calendar_timezone"] == "Asia/Almaty"
    assert "timezone_note" in out


async def test_now_omits_calendar_when_it_matches() -> None:
    out = json.loads(await _handler(tz="Europe/Belgrade", calendar_tz="Europe/Belgrade")({}))
    assert "calendar_timezone" not in out
    assert "timezone_note" not in out


async def test_now_ignores_calendar_lookup_failure() -> None:
    out = json.loads(await _handler(tz="UTC", calendar_raises=True)({}))
    assert out["timezone"] == "UTC"
    assert "calendar_timezone" not in out
