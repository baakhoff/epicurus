"""Unit tests for the MCP tool surface — module tools with a mock provider.

Uses the local provider backed by an in-memory SQLite database so the tools
are exercised end-to-end without needing a running Docker stack.

Tools are called via ``module.mcp.call_tool(name, args)`` which returns
``(content, structured)`` — the same wire path used by the agent.  The
structured dict carries a ``"result"`` key (or is the result itself if the
tool returns a plain dict); a tool that returns an entity-reference envelope
(``calendar_list_events``) serializes a :class:`ToolEnvelope` as its text
content, parsed here with :func:`_parse_envelope`.
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
from epicurus_calendar.providers.router import CollectionRouter
from epicurus_calendar.service import (
    CALENDAR_PAGE_ID,
    EVENT_KIND,
    EventNotFound,
    build_module,
    calendar_accounts,
    calendar_attachments,
    calendar_page,
    event_attachment,
    event_attachment_item,
    event_entity_ref,
    event_excerpt,
    event_hover_card,
    fetch_event,
)
from epicurus_core import Collection, CollectionPrefs, CollectionRef
from epicurus_core.contracts import ToolEnvelope


def _dt(hour: int) -> datetime:
    return datetime(2025, 6, 15, hour, 0, 0, tzinfo=UTC)


def _extract(structured: dict[str, Any] | None) -> Any:
    """Unwrap the MCP call_tool result."""
    if structured is None:
        return None
    return structured.get("result", structured)


def _parse_envelope(content: list[Any]) -> ToolEnvelope:
    """Parse the ToolEnvelope from the first text-content item of a call_tool result."""
    text = content[0].text
    return ToolEnvelope.model_validate_json(text)


def _event(**kw: Any) -> Event:
    """Build an Event with sensible defaults, overridable per test."""
    base: dict[str, Any] = {
        "id": "e1",
        "title": "Standup",
        "start": datetime(2026, 6, 15, 9, 0, tzinfo=UTC),
        "end": datetime(2026, 6, 15, 9, 30, tzinfo=UTC),
        "description": None,
        "location": None,
        "provider": "local",
    }
    base.update(kw)
    return Event(**base)


# ── Helpers ──────────────────────────────────────────────────────────────────


@pytest.fixture()
async def local_provider() -> LocalCalendarProvider:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = LocalEventStore(engine)
    await store.init()
    return LocalCalendarProvider(store=store)


# ── calendar_list_events (entity-reference envelope, ADR-0019) ────────────────


async def test_list_events_empty_has_no_refs(local_provider: LocalCalendarProvider) -> None:
    module = build_module(local_provider, tenant_id="t1")
    content, _ = await module.mcp.call_tool("calendar_list_events", {"range_days": 7})
    envelope = _parse_envelope(content)
    assert envelope.entity_refs == []
    assert "No events" in envelope.text


async def test_list_events_returns_event_chips(local_provider: LocalCalendarProvider) -> None:
    await local_provider.create_event(
        tenant_id="t1",
        title="Sprint planning",
        start=datetime.now(tz=UTC) + timedelta(hours=1),
        end=datetime.now(tz=UTC) + timedelta(hours=2),
    )
    module = build_module(local_provider, tenant_id="t1")
    content, _ = await module.mcp.call_tool("calendar_list_events", {"range_days": 1})
    envelope = _parse_envelope(content)
    assert len(envelope.entity_refs) == 1
    ref = envelope.entity_refs[0]
    assert ref.module == "calendar"
    assert ref.kind == "event"
    assert ref.title == "Sprint planning"
    assert "Sprint planning" in envelope.text
    assert "1 event" in envelope.text


async def test_list_events_clamps_range(local_provider: LocalCalendarProvider) -> None:
    module = build_module(local_provider, tenant_id="t1")
    # range_days=200 is clamped to 90; must not raise and yields a valid envelope.
    content, _ = await module.mcp.call_tool("calendar_list_events", {"range_days": 200})
    envelope = _parse_envelope(content)
    assert isinstance(envelope.entity_refs, list)


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


# ── Entity references, hover-cards & attachments (ADR-0019) ───────────────────


def test_event_entity_ref_shape() -> None:
    ref = event_entity_ref(_event(location="Room 4"))
    assert ref.ref_id == "e1"
    assert ref.module == "calendar"
    assert ref.kind == EVENT_KIND
    assert ref.title == "Standup"
    assert ref.summary is not None
    assert "09:00" in ref.summary
    assert "Room 4" in ref.summary


def test_event_hover_card_full() -> None:
    card = event_hover_card(_event(description="Daily sync", location="Room 4"))
    assert card["title"] == "Standup"
    assert card["description"] == "Daily sync"
    labels = {d["label"]: d["value"] for d in card["details"]}
    assert "When" in labels
    assert labels["Location"] == "Room 4"
    assert labels["Calendar"] == "local"
    assert card.get("href") is None


def test_event_hover_card_omits_location_when_absent() -> None:
    card = event_hover_card(_event())
    labels = [d["label"] for d in card["details"]]
    assert "Location" not in labels
    assert card["description"] == ""


def test_event_excerpt_includes_when_and_description() -> None:
    excerpt = event_excerpt(_event(description="Daily sync", location="Room 4"))
    assert "Standup" in excerpt
    assert "Room 4" in excerpt
    assert "Daily sync" in excerpt
    assert "09:00" in excerpt


def test_event_attachment_item_shape() -> None:
    assert event_attachment_item(_event()) == {
        "ref_id": "e1",
        "kind": "event",
        "title": "Standup",
    }


def test_event_attachment_payload_has_title_and_excerpt() -> None:
    payload = event_attachment(_event(description="Daily sync"))
    assert payload["title"] == "Standup"
    assert "Daily sync" in payload["excerpt"]


def test_format_when_same_day_is_compact() -> None:
    # A same-day event collapses to one date and a start-end time range.
    ref = event_entity_ref(_event())
    assert ref.summary is not None
    assert "15 Jun 2026" in ref.summary
    assert "09:00" in ref.summary and "09:30" in ref.summary


def test_format_when_multi_day_uses_arrow() -> None:
    multi = _event(end=datetime(2026, 6, 16, 10, 0, tzinfo=UTC))
    ref = event_entity_ref(multi)
    assert ref.summary is not None
    assert "→" in ref.summary


async def test_fetch_event_returns_event(local_provider: LocalCalendarProvider) -> None:
    created = await local_provider.create_event(
        tenant_id="t1", title="Review", start=_dt(14), end=_dt(15)
    )
    fetched = await fetch_event(local_provider, tenant_id="t1", ref_id=created.id)
    assert fetched.id == created.id
    assert fetched.title == "Review"


async def test_fetch_event_missing_raises(local_provider: LocalCalendarProvider) -> None:
    with pytest.raises(EventNotFound):
        await fetch_event(local_provider, tenant_id="t1", ref_id="does-not-exist")


async def test_calendar_attachments_lists_upcoming(local_provider: LocalCalendarProvider) -> None:
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    soon = await local_provider.create_event(
        tenant_id="t1", title="Soon", start=now + timedelta(hours=1), end=now + timedelta(hours=2)
    )
    past_start = now - timedelta(days=2)
    await local_provider.create_event(
        tenant_id="t1", title="Past", start=past_start, end=past_start + timedelta(hours=1)
    )
    items = await calendar_attachments(local_provider, tenant_id="t1", now=now)
    titles = [i["title"] for i in items]
    assert "Soon" in titles
    assert "Past" not in titles
    assert all(i["kind"] == "event" for i in items)
    assert any(i["ref_id"] == soon.id for i in items)


async def test_calendar_attachments_respects_limit(local_provider: LocalCalendarProvider) -> None:
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    for n in range(5):
        start = now + timedelta(days=n + 1)
        await local_provider.create_event(
            tenant_id="t1", title=f"E{n}", start=start, end=start + timedelta(hours=1)
        )
    items = await calendar_attachments(local_provider, tenant_id="t1", now=now, limit=3)
    assert len(items) == 3


# ── Google provider (mocked) ─────────────────────────────────────────────────


class _MockGoogleProvider(CalendarProvider):
    name = "google"

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def list_events(
        self, *, tenant_id: str, time_range: DateTimeRange, calendar_id: str | None = None
    ) -> list[Event]:
        return [e for e in self.events if e.start < time_range.end and e.end > time_range.start]

    async def get_event(
        self, *, tenant_id: str, event_id: str, calendar_id: str | None = None
    ) -> Event | None:
        return next((e for e in self.events if e.id == event_id), None)

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
        self,
        *,
        tenant_id: str,
        time_range: DateTimeRange,
        duration_minutes: int,
        calendar_id: str | None = None,
    ) -> list[DateTimeRange]:
        return []

    async def is_available(self, *, tenant_id: str) -> bool:
        return True

    async def list_collections(self, *, tenant_id: str) -> list[Collection]:
        return [
            Collection(account="google", collection="primary", title="me@example.com"),
            Collection(account="google", collection="team@x.com", title="Team", writable=False),
        ]


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
    content, _ = await module.mcp.call_tool("calendar_list_events", {"range_days": 1})
    envelope = _parse_envelope(content)
    assert any(r.title == "Seeded" for r in envelope.entity_refs)


async def test_google_mock_provider_get_event() -> None:
    mock_provider = _MockGoogleProvider()
    created = await mock_provider.create_event(
        tenant_id="t1",
        title="Sync",
        start=datetime(2026, 6, 15, 10, 0, tzinfo=UTC),
        end=datetime(2026, 6, 15, 11, 0, tzinfo=UTC),
    )
    found = await fetch_event(mock_provider, tenant_id="t1", ref_id=created.id)
    assert found.title == "Sync"
    assert await mock_provider.get_event(tenant_id="t1", event_id="nope") is None


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


# ── Manifest ──────────────────────────────────────────────────────────────────


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


async def test_manifest_declares_resolver_and_attachable(
    local_provider: LocalCalendarProvider,
) -> None:
    module = build_module(local_provider, tenant_id="t1")
    manifest = await module.manifest()
    assert manifest.resolver is True
    assert manifest.attachable is True


async def test_manifest_has_no_card_actions(local_provider: LocalCalendarProvider) -> None:
    # The list tool now returns an entity-ref envelope (chips), so it is not a card
    # action — the module exposes no plain-text action button (mirrors mail).
    module = build_module(local_provider, tenant_id="t1")
    manifest = await module.manifest()
    assert manifest.ui is not None
    assert manifest.ui.actions == []


async def test_manifest_version_is_0_6_0(local_provider: LocalCalendarProvider) -> None:
    module = build_module(local_provider, tenant_id="t1")
    manifest = await module.manifest()
    assert manifest.version == "0.6.0"


async def test_manifest_declares_calendar_oauth_scope(
    local_provider: LocalCalendarProvider,
) -> None:
    module = build_module(local_provider, tenant_id="t1")
    manifest = await module.manifest()
    assert manifest.oauth_scopes == {"google": ["https://www.googleapis.com/auth/calendar"]}


async def test_manifest_declares_collections_and_drops_provider_dropdown(
    local_provider: LocalCalendarProvider,
) -> None:
    # The account/collection model (ADR-0030): a collections spec replaces the old
    # local/google provider dropdown (config_schema is gone).
    module = build_module(local_provider, tenant_id="t1")
    manifest = await module.manifest()
    assert manifest.collections is not None
    assert manifest.collections.noun == "calendar"
    assert manifest.collections.multi is True
    assert manifest.collections.providers == ["google"]
    assert manifest.ui is not None
    assert manifest.ui.config_schema is None


# ── Connected accounts (ADR-0030) ─────────────────────────────────────────────


async def test_calendar_accounts_lists_connected_google_with_collections() -> None:
    view = await calendar_accounts({"google": _MockGoogleProvider()}, tenant_id="t1")
    assert view.noun == "calendar"
    assert view.multi is True
    assert len(view.accounts) == 1
    account = view.accounts[0]
    assert account.account == "google"
    assert account.label == "Google"
    assert account.connected is True
    assert [c.collection for c in account.collections] == ["primary", "team@x.com"]


class _DisconnectedGoogle(_MockGoogleProvider):
    async def is_available(self, *, tenant_id: str) -> bool:
        return False


async def test_calendar_accounts_omits_collections_when_disconnected() -> None:
    view = await calendar_accounts({"google": _DisconnectedGoogle()}, tenant_id="t1")
    account = view.accounts[0]
    assert account.connected is False
    assert account.collections == []


# ── CollectionRouter (ADR-0030) ───────────────────────────────────────────────


class _StaticPrefs:
    """A prefs source returning a fixed selection (stands in for the PlatformClient)."""

    def __init__(self, prefs: CollectionPrefs) -> None:
        self._prefs = prefs

    async def get_collections(self) -> CollectionPrefs:
        return self._prefs


def _google_ref(collection: str) -> CollectionRef:
    return CollectionRef(account="google", collection=collection)


async def _router(prefs: CollectionPrefs) -> tuple[CollectionRouter, LocalCalendarProvider, Any]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = LocalEventStore(engine)
    await store.init()
    local = LocalCalendarProvider(store=store)
    google = _MockGoogleProvider()
    router = CollectionRouter(local=local, external={"google": google}, prefs=_StaticPrefs(prefs))
    return router, local, google


async def test_router_reads_local_when_nothing_enabled() -> None:
    router, local, _ = await _router(CollectionPrefs())
    await local.create_event(tenant_id="t1", title="Local one", start=_dt(9), end=_dt(10))
    events = await router.list_events(
        tenant_id="t1", time_range=DateTimeRange(start=_dt(0), end=_dt(23))
    )
    assert [e.title for e in events] == ["Local one"]


async def test_router_overlays_enabled_collections() -> None:
    router, local, google = await _router(CollectionPrefs(enabled=[_google_ref("primary")]))
    await google.create_event(tenant_id="t1", title="From Google", start=_dt(11), end=_dt(12))
    # A local event exists but local is not enabled, so it must not appear in the overlay.
    await local.create_event(tenant_id="t1", title="Local hidden", start=_dt(9), end=_dt(10))
    events = await router.list_events(
        tenant_id="t1", time_range=DateTimeRange(start=_dt(0), end=_dt(23))
    )
    assert [e.title for e in events] == ["From Google"]


async def test_router_writes_to_active_collection() -> None:
    router, _, google = await _router(
        CollectionPrefs(enabled=[_google_ref("primary")], active=_google_ref("primary"))
    )
    created = await router.create_event(tenant_id="t1", title="Routed", start=_dt(14), end=_dt(15))
    assert created.provider == "google"
    assert google.events[-1].title == "Routed"


async def test_router_writes_local_when_no_active() -> None:
    router, local, _ = await _router(CollectionPrefs())
    created = await router.create_event(tenant_id="t1", title="Default", start=_dt(14), end=_dt(15))
    assert created.provider == "local"
    assert await local.get_event(tenant_id="t1", event_id=created.id) is not None


async def test_router_get_event_searches_enabled_and_local() -> None:
    router, _local, google = await _router(CollectionPrefs(enabled=[_google_ref("primary")]))
    g = await google.create_event(tenant_id="t1", title="G", start=_dt(9), end=_dt(10))
    found = await router.get_event(tenant_id="t1", event_id=g.id)
    assert found is not None
    assert found.title == "G"
    assert await router.get_event(tenant_id="t1", event_id="missing") is None


async def test_router_falls_back_to_local_when_prefs_unavailable() -> None:
    class _BrokenPrefs:
        async def get_collections(self) -> CollectionPrefs:
            raise RuntimeError("core down")

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = LocalEventStore(engine)
    await store.init()
    local = LocalCalendarProvider(store=store)
    router = CollectionRouter(local=local, external={}, prefs=_BrokenPrefs())
    await local.create_event(tenant_id="t1", title="Survives", start=_dt(9), end=_dt(10))
    events = await router.list_events(
        tenant_id="t1", time_range=DateTimeRange(start=_dt(0), end=_dt(23))
    )
    assert [e.title for e in events] == ["Survives"]


async def test_router_skips_a_failing_source_on_overlay() -> None:
    # One enabled calendar erroring (e.g. Google just disconnected) must not blank the
    # whole overlay — the other sources still render (#209).
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = LocalEventStore(engine)
    await store.init()
    local = LocalCalendarProvider(store=store)
    await local.create_event(tenant_id="t1", title="Local ok", start=_dt(9), end=_dt(10))

    class _FailingGoogle(_MockGoogleProvider):
        async def list_events(
            self, *, tenant_id: str, time_range: DateTimeRange, calendar_id: str | None = None
        ) -> list[Event]:
            raise RuntimeError("google down")

    prefs = CollectionPrefs(enabled=[CollectionRef(account="local"), _google_ref("primary")])
    router = CollectionRouter(
        local=local, external={"google": _FailingGoogle()}, prefs=_StaticPrefs(prefs)
    )
    events = await router.list_events(
        tenant_id="t1", time_range=DateTimeRange(start=_dt(0), end=_dt(23))
    )
    assert [e.title for e in events] == ["Local ok"]


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
    pages = {p.id: p for p in manifest.pages}
    assert CALENDAR_PAGE_ID in pages
    assert pages[CALENDAR_PAGE_ID].archetype == "calendar"
    assert pages[CALENDAR_PAGE_ID].icon == "calendar"
