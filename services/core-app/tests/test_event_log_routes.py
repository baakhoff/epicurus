"""Tests for the raw events feed's HTTP surface — the snapshot endpoint and the SSE tail.

These drive the app through ``httpx.ASGITransport`` rather than ``TestClient``, matching
``test_agent_routes.py``. It is not a style preference: ``TestClient`` runs the app in a
*separate thread's* event loop via a portal, while the aiosqlite engine here belongs to the
test's loop — a combination that deadlocks the moment a handler touches the store mid-stream.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core import EntityRef, EventEnvelope
from epicurus_core_app.event_log import EventIntake, EventLogStore, LoggedEvent
from epicurus_core_app.event_log_routes import create_event_log_router

TENANT = "local"


def _envelope(
    *,
    tenant: str = TENANT,
    module: str = "echo",
    event_type: str = "echo.pinged",
    dedup_key: str = "k1",
) -> EventEnvelope:
    return EventEnvelope(
        tenant_id=tenant,
        module=module,
        type=event_type,
        occurred_at=datetime.now(UTC),
        dedup_key=dedup_key,
        entity_ref=EntityRef(ref_id=dedup_key, module=module, kind="ping", title="hi"),
        payload={"n": 1},
    )


class _FakeBus:
    async def subscribe_any_tenant(
        self, subject: str, handler: object, *, queue: str = ""
    ) -> object:
        class _Sub:
            async def unsubscribe(self) -> None: ...

        return _Sub()


async def _fresh() -> tuple[EventLogStore, EventIntake]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    store = EventLogStore(engine)
    await store.init()
    return store, EventIntake(store, _FakeBus())  # type: ignore[arg-type]


def _client(store: EventLogStore, intake: EventIntake) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(create_event_log_router(intake, store, default_tenant=TENANT))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_recent_returns_events_newest_first() -> None:
    store, intake = await _fresh()
    for i in range(3):
        await store.append(_envelope(dedup_key=f"k{i}"))
    async with _client(store, intake) as client:
        body = (await client.get("/platform/v1/events")).json()
    assert [e["dedup_key"] for e in body] == ["k2", "k1", "k0"]
    assert body[0]["entity_ref"]["ref_id"] == "k2"  # the chip renders with no module code
    assert body[0]["payload"] == {"n": 1}


async def test_recent_filters_by_module_and_type() -> None:
    store, intake = await _fresh()
    await store.append(_envelope(module="echo", dedup_key="e"))
    await store.append(_envelope(module="mail", event_type="mail.received", dedup_key="m"))
    async with _client(store, intake) as client:
        by_module = (await client.get("/platform/v1/events", params={"module": "mail"})).json()
        by_type = (await client.get("/platform/v1/events", params={"type": "echo.pinged"})).json()
    assert [e["dedup_key"] for e in by_module] == ["m"]
    assert [e["dedup_key"] for e in by_type] == ["e"]


async def test_recent_is_tenant_scoped_and_defaults_to_the_default_tenant() -> None:
    store, intake = await _fresh()
    await store.append(_envelope(tenant=TENANT, dedup_key="mine"))
    await store.append(_envelope(tenant="other", dedup_key="theirs"))
    async with _client(store, intake) as client:
        default = (await client.get("/platform/v1/events")).json()
        explicit = (await client.get("/platform/v1/events", params={"tenant_id": "other"})).json()
    assert [e["dedup_key"] for e in default] == ["mine"]
    assert [e["dedup_key"] for e in explicit] == ["theirs"]


async def test_recent_rejects_an_out_of_range_limit() -> None:
    store, intake = await _fresh()
    async with _client(store, intake) as client:
        assert (await client.get("/platform/v1/events", params={"limit": 0})).status_code == 422
        assert (await client.get("/platform/v1/events", params={"limit": 5000})).status_code == 422


async def test_recent_honours_limit() -> None:
    store, intake = await _fresh()
    for i in range(5):
        await store.append(_envelope(dedup_key=f"k{i}"))
    async with _client(store, intake) as client:
        body = (await client.get("/platform/v1/events", params={"limit": 2})).json()
    assert len(body) == 2


# ── the SSE tail ─────────────────────────────────────────────────────────────
#
# The real feed never ends, and both in-process ASGI clients (httpx's ASGITransport and
# starlette's TestClient) drive the app to *completion* before handing back a response —
# so pointing either at the live endpoint deadlocks rather than streaming. That is a
# property of the test clients, not of the code.
#
# So the layers are tested at their own seams: EventIntake.stream's semantics (history
# order, live delivery, filtering, subscriber cleanup) are driven directly in
# test_event_log.py, and what the *router* adds — status, headers, SSE framing, query
# plumbing — is tested here against a finite stub. The genuinely-infinite path over real
# HTTP is what `task smoke` asserts end-to-end.


class _FiniteIntake:
    """An intake whose feed ends, so an ASGI test client can read it to completion."""

    def __init__(self, entries: list[LoggedEvent]) -> None:
        self.entries = entries
        self.calls: list[dict[str, str | None]] = []

    async def stream(
        self,
        *,
        tenant: str,
        module: str | None = None,
        event_type: str | None = None,
    ) -> AsyncIterator[LoggedEvent]:
        self.calls.append({"tenant": tenant, "module": module, "event_type": event_type})
        for entry in self.entries:
            yield entry


def _stub_client(intake: _FiniteIntake, store: EventLogStore) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(
        create_event_log_router(intake, store, default_tenant=TENANT)  # type: ignore[arg-type]
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _parse_frames(text: str) -> list[str]:
    return [frame for frame in text.split("\n\n") if frame.strip()]


async def test_stream_frames_each_event_as_sse() -> None:
    store, _intake = await _fresh()
    stored = await store.append(_envelope(dedup_key="k0"))
    assert stored is not None
    intake = _FiniteIntake([stored])
    async with _stub_client(intake, store) as client:
        response = await client.get("/platform/v1/events/stream")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    # X-Accel-Buffering: no — without it a proxy buffers the tail into uselessness.
    assert response.headers["x-accel-buffering"] == "no"
    frames = _parse_frames(response.text)
    assert len(frames) == 1
    assert frames[0].startswith("event: module_event\ndata: {")
    assert '"dedup_key":"k0"' in frames[0]
    assert '"entity_ref":' in frames[0]  # the chip travels with the row


async def test_stream_preserves_feed_order() -> None:
    store, _intake = await _fresh()
    first = await store.append(_envelope(dedup_key="k0"))
    second = await store.append(_envelope(dedup_key="k1"))
    assert first is not None and second is not None
    async with _stub_client(_FiniteIntake([first, second]), store) as client:
        response = await client.get("/platform/v1/events/stream")
    frames = _parse_frames(response.text)
    assert '"dedup_key":"k0"' in frames[0]
    assert '"dedup_key":"k1"' in frames[1]


async def test_stream_passes_filters_and_tenant_through() -> None:
    store, _intake = await _fresh()
    intake = _FiniteIntake([])
    async with _stub_client(intake, store) as client:
        await client.get("/platform/v1/events/stream")
        await client.get(
            "/platform/v1/events/stream",
            params={"tenant_id": "other", "module": "mail", "type": "mail.received"},
        )
    assert intake.calls[0] == {"tenant": TENANT, "module": None, "event_type": None}
    assert intake.calls[1] == {"tenant": "other", "module": "mail", "event_type": "mail.received"}
