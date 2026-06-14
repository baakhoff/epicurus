"""Unit tests for the MCP tool surface — module tools with a mock provider.

Uses the local provider backed by an in-memory SQLite database so the tools
are exercised end-to-end without needing a running Docker stack.

Tools are called via ``module.mcp.call_tool(name, args)`` which returns
``(content, structured)`` — the same wire path used by the agent.  The
structured dict carries a ``"result"`` key (or is the result itself if the
tool returns a plain dict).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_calendar.db import LocalEventStore
from epicurus_calendar.models import DateTimeRange, Event
from epicurus_calendar.providers.base import CalendarProvider
from epicurus_calendar.providers.local import LocalCalendarProvider
from epicurus_calendar.service import CALENDAR_PAGE_ID, build_module, calendar_page


def _dt(hour: int) -> datetime:
    return datetime(2025, 6, 15, hour, 0, 0, tzinfo=UTC)


def _extract(structured: dict[str, Any] | None) -> Any:
    """Unwrap the MCP call_tool result."""
    if structured is None:
        return None
    return structured.get("result", structured)


# ── Helpers ──────────────────────────────────────────────────────────────────


@pytest.fixture()
async def local_provider() -> LocalCalendarProvider:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = LocalEventStore(engine)
    await store.init()
    return LocalCalendarProvider(store=store)


# ── calendar_list_events ─────────────────────────────────────────────────────


async def test_list_events_empty(local_provider: LocalCalendarProvider) -> None:
    module = build_module(local_provider, tenant_id="t1")
    _content, structured = await module.mcp.call_tool("calendar_list_events", {"range_days": 7})
    assert _extract(structured) == []


async def test_list_events_returns_created(local_provider: LocalCalendarProvider) -> None:
    await local_provider.create_event(
        tenant_id="t1",
        title="Sprint planning",
        start=datetime.now(tz=UTC) + timedelta(hours=1),
        end=datetime.now(tz=UTC) + timedelta(hours=2),
    )
    module = build_module(local_provider, tenant_id="t1")
    _content, structured = await module.mcp.call_tool("calendar_list_events", {"range_days": 1})
    result = _extract(structured)
    assert len(result) == 1
    assert result[0]["title"] == "Sprint planning"
    assert result[0]["provider"] == "local"


async def test_list_events_clamps_range(local_provider: LocalCalendarProvider) -> None:
    module = build_module(local_provider, tenant_id="t1")
    # range_days=200 is clamped to 90; must not raise
    _content, structured = await module.mcp.call_tool("calendar_list_events", {"range_days": 200})
    assert isinstance(_extract(structured), list)


# ── calendar_create_event ────────────────────────────────────────────────────


async def test_create_event(local_provider: LocalCalendarProvider) -> None:
    module = build_module(local_provider, tenant_id="t1")
    _content, structured = await module.mcp.call_tool(
        "calendar_create_event",
        {
            "title": "Team lunch",
            "start": "2025-06-15T12:00:00+00:00",
            "end": "2025-06-15T13:00:00+00:00",
            "description": "Monthly team lunch",
            "location": "The Italian Place",
        },
    )
    result = _extract(structured)
    assert result["title"] == "Team lunch"
    assert result["description"] == "Monthly team lunch"
    assert result["location"] == "The Italian Place"
    assert result["provider"] == "local"
    assert "id" in result


async def test_create_event_minimal(local_provider: LocalCalendarProvider) -> None:
    module = build_module(local_provider, tenant_id="t1")
    _content, structured = await module.mcp.call_tool(
        "calendar_create_event",
        {
            "title": "Quick call",
            "start": "2025-06-15T09:00:00+00:00",
            "end": "2025-06-15T09:30:00+00:00",
        },
    )
    result = _extract(structured)
    assert result["title"] == "Quick call"
    assert result.get("description") is None


# ── calendar_find_free ───────────────────────────────────────────────────────


async def test_find_free_no_events(local_provider: LocalCalendarProvider) -> None:
    module = build_module(local_provider, tenant_id="t1")
    _content, structured = await module.mcp.call_tool(
        "calendar_find_free", {"duration_minutes": 60, "range_days": 1}
    )
    result = _extract(structured)
    # With no events the whole window is free; at least one slot must be returned.
    assert isinstance(result, list)
    assert len(result) >= 1


async def test_find_free_clamps_args(local_provider: LocalCalendarProvider) -> None:
    module = build_module(local_provider, tenant_id="t1")
    # Boundary values must not raise.
    _content, structured = await module.mcp.call_tool(
        "calendar_find_free", {"duration_minutes": 0, "range_days": 0}
    )
    assert isinstance(_extract(structured), list)


# ── Google provider (mocked) ─────────────────────────────────────────────────


class _MockGoogleProvider(CalendarProvider):
    name = "google"

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def list_events(self, *, tenant_id: str, time_range: DateTimeRange) -> list[Event]:
        return [e for e in self.events if e.start < time_range.end and e.end > time_range.start]

    async def create_event(
        self,
        *,
        tenant_id: str,
        title: str,
        start: datetime,
        end: datetime,
        description: str | None = None,
        location: str | None = None,
    ) -> Event:
        event = Event(
            id="g-1",
            title=title,
            start=start,
            end=end,
            description=description,
            location=location,
            provider="google",
        )
        self.events.append(event)
        return event

    async def find_free_slots(
        self, *, tenant_id: str, time_range: DateTimeRange, duration_minutes: int
    ) -> list[DateTimeRange]:
        return []

    async def is_available(self, *, tenant_id: str) -> bool:
        return True


async def test_google_mock_provider_creates_event() -> None:
    mock_provider = _MockGoogleProvider()
    module = build_module(mock_provider, tenant_id="t1")
    _content, structured = await module.mcp.call_tool(
        "calendar_create_event",
        {
            "title": "Video call",
            "start": "2025-06-15T15:00:00+00:00",
            "end": "2025-06-15T16:00:00+00:00",
        },
    )
    result = _extract(structured)
    assert result["title"] == "Video call"
    assert result["provider"] == "google"


async def test_google_mock_provider_lists_events() -> None:
    mock_provider = _MockGoogleProvider()
    # Pre-seed with an event in the future relative to "now" in list_events.
    future = datetime.now(tz=UTC) + timedelta(hours=2)
    mock_provider.events = [
        Event(
            id="g-pre",
            title="Seeded",
            start=future,
            end=future + timedelta(hours=1),
            provider="google",
        )
    ]
    module = build_module(mock_provider, tenant_id="t1")
    _content, structured = await module.mcp.call_tool("calendar_list_events", {"range_days": 1})
    result = _extract(structured)
    assert any(r["title"] == "Seeded" for r in result)


async def test_both_providers_same_tool_interface() -> None:
    """Confirm local and Google providers are interchangeable behind the tools."""
    mock_google = _MockGoogleProvider()

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = LocalEventStore(engine)
    await store.init()
    local = LocalCalendarProvider(store=store)

    for prov in (local, mock_google):
        module = build_module(prov, tenant_id="t1")
        _content, structured = await module.mcp.call_tool(
            "calendar_create_event",
            {
                "title": "Interface test",
                "start": "2025-06-15T10:00:00+00:00",
                "end": "2025-06-15T11:00:00+00:00",
            },
        )
        result = _extract(structured)
        assert result["title"] == "Interface test"
        assert result["provider"] == prov.name


async def test_manifest_declares_tools_and_ui() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = LocalEventStore(engine)
    await store.init()
    local = LocalCalendarProvider(store=store)
    module = build_module(local, tenant_id="t1")
    manifest = await module.manifest()
    tool_names = {t.name for t in manifest.tools}
    assert "calendar_list_events" in tool_names
    assert "calendar_create_event" in tool_names
    assert "calendar_find_free" in tool_names
    assert manifest.ui is not None
    assert manifest.ui.status_url == "/status"
    assert manifest.ui.icon == "calendar"


# ── calendar_page (the `calendar` archetype data, ADR-0018) ───────────────────


async def test_calendar_page_shape_when_empty(local_provider: LocalCalendarProvider) -> None:
    data = await calendar_page(local_provider, tenant_id="t1")
    assert data["title"] == "Calendar"
    assert data["provider"] == "local"
    assert data["events"] == []
    assert set(data["range"]) == {"start", "end"}


async def test_calendar_page_lists_only_events_in_window(
    local_provider: LocalCalendarProvider,
) -> None:
    near = datetime(2026, 6, 15, 9, 0, tzinfo=UTC)
    await local_provider.create_event(
        tenant_id="t1", title="Standup", start=near, end=near + timedelta(minutes=30)
    )
    far = near + timedelta(days=40)
    await local_provider.create_event(
        tenant_id="t1", title="Far away", start=far, end=far + timedelta(hours=1)
    )
    data = await calendar_page(
        local_provider,
        tenant_id="t1",
        start="2026-06-01T00:00:00+00:00",
        end="2026-07-01T00:00:00+00:00",
    )
    assert [e["title"] for e in data["events"]] == ["Standup"]


async def test_calendar_page_defaults_to_current_month(
    local_provider: LocalCalendarProvider,
) -> None:
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    in_june = datetime(2026, 6, 20, 10, 0, tzinfo=UTC)
    await local_provider.create_event(
        tenant_id="t1", title="This month", start=in_june, end=in_june + timedelta(hours=1)
    )
    data = await calendar_page(local_provider, tenant_id="t1", now=now)
    assert data["range"]["start"].startswith("2026-06-01")
    assert data["range"]["end"].startswith("2026-07-01")
    assert [e["title"] for e in data["events"]] == ["This month"]


async def test_calendar_page_default_window_rolls_over_december(
    local_provider: LocalCalendarProvider,
) -> None:
    now = datetime(2026, 12, 10, 12, 0, tzinfo=UTC)
    data = await calendar_page(local_provider, tenant_id="t1", now=now)
    assert data["range"]["start"].startswith("2026-12-01")
    assert data["range"]["end"].startswith("2027-01-01")


async def test_calendar_page_rejects_unparseable_dates(
    local_provider: LocalCalendarProvider,
) -> None:
    with pytest.raises(ValueError):
        await calendar_page(
            local_provider, tenant_id="t1", start="nonsense", end="2026-07-01T00:00:00+00:00"
        )


async def test_calendar_page_rejects_inverted_range(
    local_provider: LocalCalendarProvider,
) -> None:
    with pytest.raises(ValueError):
        await calendar_page(
            local_provider,
            tenant_id="t1",
            start="2026-07-01T00:00:00+00:00",
            end="2026-06-01T00:00:00+00:00",
        )


async def test_calendar_page_clamps_an_overwide_window(
    local_provider: LocalCalendarProvider,
) -> None:
    data = await calendar_page(
        local_provider,
        tenant_id="t1",
        start="2026-01-01T00:00:00+00:00",
        end="2027-01-01T00:00:00+00:00",
    )
    span = datetime.fromisoformat(data["range"]["end"]) - datetime.fromisoformat(
        data["range"]["start"]
    )
    assert span <= timedelta(days=92)


async def test_calendar_page_reads_naive_iso_as_utc(
    local_provider: LocalCalendarProvider,
) -> None:
    # A timestamp with no offset is read as UTC, not rejected.
    data = await calendar_page(
        local_provider, tenant_id="t1", start="2026-06-01T00:00:00", end="2026-06-30T00:00:00"
    )
    assert data["range"]["start"].startswith("2026-06-01T00:00:00+00:00")


async def test_manifest_declares_calendar_page(
    local_provider: LocalCalendarProvider,
) -> None:
    module = build_module(local_provider, tenant_id="t1")
    manifest = await module.manifest()
    assert manifest.version == "0.2.0"
    pages = {p.id: p for p in manifest.pages}
    assert CALENDAR_PAGE_ID in pages
    assert pages[CALENDAR_PAGE_ID].archetype == "calendar"
    assert pages[CALENDAR_PAGE_ID].icon == "calendar"
