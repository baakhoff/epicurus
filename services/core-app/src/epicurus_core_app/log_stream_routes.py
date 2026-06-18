"""HTTP routes for the live log stream endpoint (ADR-0031).

Exposes ``GET /platform/v1/logs/stream`` as a Server-Sent Events feed.  Up to
``LogBuffer.MAX_HISTORY`` (200) buffered entries are replayed first; then live
entries trickle in as they are emitted.

Client-disconnect handling: the log buffer's async generator uses a 1-second
polling loop (``asyncio.wait_for(q.get(), 1.0)``).  When no entry arrives
within that window the generator yields control briefly, allowing the
``StreamingResponse`` machinery to detect a closed connection and propagate
``GeneratorExit`` / ``CancelledError`` into the generator.  This ensures the
server-side task terminates within ~1 s after the browser tab closes.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from epicurus_core_app.log_stream import LogBuffer

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}


def create_log_stream_router(buf: LogBuffer) -> APIRouter:
    """Return a router bound to *buf*."""
    router = APIRouter(prefix="/platform/v1/logs", tags=["logs"])

    @router.get("/stream")
    async def log_stream(
        level: str | None = None,
        service: str | None = None,
    ) -> StreamingResponse:
        """Stream structured log entries as SSE.

        Query params:
        - ``level``: minimum level to emit (debug/info/warning/error/critical).
          Defaults to ``info``.
        - ``service``: optional prefix filter on the ``service`` field.

        Each SSE frame has ``event: log`` and ``data: <LogEntry JSON>``.
        Up to 200 history entries are sent first, then live entries follow.
        """

        async def events() -> AsyncGenerator[str, None]:
            async for entry in buf.stream(level, service):
                yield f"event: log\ndata: {entry.model_dump_json()}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream", headers=_SSE_HEADERS)

    return router
