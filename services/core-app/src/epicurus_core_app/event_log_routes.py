"""HTTP routes for the module event spine — the raw events feed (ADR-0031's second surface).

``GET /platform/v1/events/stream`` is a Server-Sent Events tail of the durable log: up to
``FEED_HISTORY`` recent events replay first, then live ones trickle in. ``GET
/platform/v1/events`` is the same data as a plain page, for anything that wants a snapshot
rather than a subscription.

Client-disconnect handling matches the log stream's: the underlying generator polls its
live queue with a 1-second timeout, so when a browser tab closes, ``StreamingResponse``
notices within ~1s and propagates ``GeneratorExit``/``CancelledError`` into the generator,
which unregisters the subscriber in its ``finally``. Without that periodic yield an idle
feed would block on an empty queue forever and leak a subscriber per closed tab.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from epicurus_core_app.event_log import FEED_HISTORY, EventIntake, EventLogStore, LoggedEvent

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}


def create_event_log_router(
    intake: EventIntake,
    store: EventLogStore,
    *,
    default_tenant: str = "local",
) -> APIRouter:
    """Return a router bound to the live *intake* and its durable *store*."""
    router = APIRouter(prefix="/platform/v1/events", tags=["events"])

    @router.get("", response_model=list[LoggedEvent])
    async def recent_events(
        tenant_id: str | None = Query(None),
        module: str | None = Query(None),
        type: str | None = Query(None),
        limit: int = Query(FEED_HISTORY, ge=1, le=1000),
    ) -> list[LoggedEvent]:
        """The most recent events first, optionally filtered by module and/or type."""
        return await store.recent(
            tenant=tenant_id or default_tenant,
            limit=limit,
            module=module,
            event_type=type,
        )

    @router.get("/stream")
    async def event_stream(
        tenant_id: str | None = Query(None),
        module: str | None = Query(None),
        type: str | None = Query(None),
    ) -> StreamingResponse:
        """Tail the event log as SSE.

        Query params: ``module`` and ``type`` narrow the feed; both are exact matches.
        Each frame is ``event: module_event`` with a ``LoggedEvent`` JSON body. History
        replays oldest-first, then live events follow in arrival order.
        """
        tenant = tenant_id or default_tenant

        async def events() -> AsyncGenerator[str, None]:
            async for entry in intake.stream(tenant=tenant, module=module, event_type=type):
                yield f"event: module_event\ndata: {entry.model_dump_json()}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream", headers=_SSE_HEADERS)

    return router


__all__ = ["create_event_log_router"]
