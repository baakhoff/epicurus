"""Tests for the core built-in `now` (ADR-0039) and `remember` (ADR-0045) tools."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from epicurus_core_app.agent.builtins import (
    NOW_SPEC,
    REMEMBER_SPEC,
    make_now_handler,
    make_remember_handler,
)
from epicurus_core_app.memory.facts import UserFact

# Built-in handlers take (arguments, tenant); `now` ignores the tenant.
Handler = Callable[[dict[str, Any], str], Awaitable[str]]


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
    out = json.loads(await _handler(tz="Europe/Belgrade")({}, "local"))
    assert out["timezone"] == "Europe/Belgrade"
    assert "datetime" in out
    assert "utc" in out
    assert "weekday" in out


async def test_now_timezone_argument_overrides_setting() -> None:
    out = json.loads(await _handler(tz="UTC")({"timezone": "Asia/Tokyo"}, "local"))
    assert out["timezone"] == "Asia/Tokyo"


async def test_now_invalid_timezone_falls_back_to_utc() -> None:
    out = json.loads(await _handler(tz="Not/AZone")({}, "local"))
    assert out["timezone"] == "UTC"


async def test_now_reports_calendar_timezone_mismatch() -> None:
    out = json.loads(await _handler(tz="Europe/Belgrade", calendar_tz="Asia/Almaty")({}, "local"))
    assert out["calendar_timezone"] == "Asia/Almaty"
    assert "timezone_note" in out


async def test_now_omits_calendar_when_it_matches() -> None:
    handler = _handler(tz="Europe/Belgrade", calendar_tz="Europe/Belgrade")
    out = json.loads(await handler({}, "local"))
    assert "calendar_timezone" not in out
    assert "timezone_note" not in out


async def test_now_ignores_calendar_lookup_failure() -> None:
    out = json.loads(await _handler(tz="UTC", calendar_raises=True)({}, "local"))
    assert out["timezone"] == "UTC"
    assert "calendar_timezone" not in out


# ── remember (ADR-0045) ───────────────────────────────────────────────────────


class _FakeFactWriter:
    """Records remember_fact calls; ``duplicate`` makes the next save a no-op (returns None)."""

    def __init__(self, *, duplicate: bool = False, raises: bool = False) -> None:
        self._duplicate = duplicate
        self._raises = raises
        self.saved: list[tuple[str, str, str]] = []  # tenant, text, source

    async def remember_fact(
        self, *, tenant: str, text: str, source: str = "auto"
    ) -> UserFact | None:
        if self._raises:
            raise RuntimeError("qdrant down")
        self.saved.append((tenant, text, source))
        if self._duplicate:
            return None
        return UserFact(id="f1", text=text, source=source)


def test_remember_spec_shape() -> None:
    assert REMEMBER_SPEC["function"]["name"] == "remember"
    assert REMEMBER_SPEC["function"]["parameters"]["required"] == ["fact"]


async def test_remember_saves_the_fact_for_the_calling_tenant() -> None:
    writer = _FakeFactWriter()
    handler = make_remember_handler(writer)
    out = await handler({"fact": "Prefers metric units"}, "t1")
    assert writer.saved == [("t1", "Prefers metric units", "tool")]
    assert "Saved to memory" in out


async def test_remember_reports_a_duplicate_without_re_saving() -> None:
    writer = _FakeFactWriter(duplicate=True)
    out = await make_remember_handler(writer)({"fact": "Already known"}, "t1")
    assert "Already in memory" in out


async def test_remember_requires_a_fact() -> None:
    writer = _FakeFactWriter()
    out = await make_remember_handler(writer)({"fact": "   "}, "t1")
    assert out.startswith("error:")
    assert writer.saved == []


async def test_remember_surfaces_a_storage_failure_as_an_error() -> None:
    out = await make_remember_handler(_FakeFactWriter(raises=True))({"fact": "x"}, "t1")
    assert out.startswith("error:")
