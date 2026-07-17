"""Live log stream — a structlog ring-buffer processor + asyncio queue fan-out.

The ``LogBuffer`` sits in the structlog processor chain (injected via
``configure_logging(extra_processors=[...])``). Every log event is captured into a
capped ring buffer and fanned out to active SSE subscribers. History is replayed
first, then live entries trickle in — so a freshly opened console tab catches up
without a page refresh.

Security: keys whose name looks like a credential are stripped from the ``context``
dict before any entry leaves this module, so the stream never surfaces tokens,
secrets, or API keys. The rule itself lives in :mod:`epicurus_core.redaction` — the
raw events feed (the spine's console, ADR-0031's second surface) applies the same one,
and a security rule kept in two places is a security rule that drifts.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import Mapping, MutableMapping
from typing import Any

from pydantic import BaseModel

from epicurus_core.redaction import is_secret_key

_LEVELS = ["debug", "info", "warning", "error", "critical"]
_DEFAULT_LEVEL_IDX = _LEVELS.index("info")


class LogEntry(BaseModel):
    ts: str
    level: str
    service: str
    message: str
    context: dict[str, Any]


class LogBuffer:
    """Thread-safe (asyncio-safe) ring buffer + subscriber fan-out for structured logs."""

    MAX_HISTORY = 200

    def __init__(self) -> None:
        self._history: deque[LogEntry] = deque(maxlen=self.MAX_HISTORY)
        self._subscribers: list[asyncio.Queue[LogEntry]] = []

    def processor(
        self,
        logger: Any,
        method: str,
        event_dict: MutableMapping[str, Any],
    ) -> Mapping[str, Any] | str | bytes | bytearray | tuple[Any, ...]:
        """Structlog processor — capture each event into the ring buffer and fan it out.

        This is injected BEFORE the renderer, so the raw event_dict still has all
        structured fields. The processor must return ``event_dict`` unchanged so the
        chain continues to the renderer.
        """
        level = str(event_dict.get("level") or method or "info")
        service = str(event_dict.get("service") or event_dict.get("logger") or "")
        message = str(event_dict.get("event", ""))
        ts = str(event_dict.get("timestamp", ""))
        context = {
            k: v
            for k, v in event_dict.items()
            if k not in {"event", "level", "timestamp", "service", "logger", "_record"}
            and not is_secret_key(k)
        }
        entry = LogEntry(ts=ts, level=level, service=service, message=message, context=context)
        self._history.append(entry)
        for q in self._subscribers:
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(entry)
        return event_dict

    async def stream(
        self,
        min_level: str | None,
        service_prefix: str | None,
    ) -> Any:
        """Async generator: yield buffered history first, then live entries.

        Args:
            min_level: minimum level name to emit (inclusive).  ``None`` or an
                unrecognised value defaults to "info".
            service_prefix: if set, only emit entries whose ``service`` field starts
                with this string.
        """
        if min_level and min_level in _LEVELS:
            min_idx = _LEVELS.index(min_level)
        else:
            min_idx = _DEFAULT_LEVEL_IDX

        def _matches(entry: LogEntry) -> bool:
            try:
                lvl_idx = _LEVELS.index(entry.level)
            except ValueError:
                lvl_idx = 1  # treat unknown as info
            return lvl_idx >= min_idx and (
                not service_prefix or entry.service.startswith(service_prefix)
            )

        # Replay buffered history first so a new subscriber sees recent context.
        for entry in list(self._history):
            if _matches(entry):
                yield entry

        # Subscribe and stream live entries.  We poll with a short timeout so
        # callers that check ``request.is_disconnected()`` can break the outer
        # loop promptly without waiting for the next event.
        q: asyncio.Queue[LogEntry] = asyncio.Queue(maxsize=500)
        self._subscribers.append(q)
        try:
            while True:
                try:
                    entry = await asyncio.wait_for(q.get(), timeout=1.0)
                except TimeoutError:
                    # No new entry — yield control so the caller can check disconnect.
                    await asyncio.sleep(0)
                    continue
                if _matches(entry):
                    yield entry
        finally:
            with contextlib.suppress(ValueError):
                self._subscribers.remove(q)
