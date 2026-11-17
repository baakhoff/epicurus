"""Live fan-out of session events to streaming connections.

Each connection gets its own bounded queue. Publishing never waits: when a connection's
queue is full its subscription is marked *overflowed* and dropped from the fan-out, so one
slow reader can never stall the session or the other readers. The stream then ends with a
``stream.overflow`` frame naming the last sequence number it delivered, and the client
reconnects with ``?after=<that seq>`` — the durable log has everything it missed.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

from epicurus_hermarchus.store import Event

__all__ = ["Broker", "Subscription"]


class Subscription:
    def __init__(self, session_id: str, maxsize: int) -> None:
        self.session_id = session_id
        self.queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=maxsize)
        self.overflowed = False
        self.closed = False


class Broker:
    def __init__(self, on_overflow: Callable[[], None] | None = None) -> None:
        self._subs: dict[str, set[Subscription]] = {}
        self._on_overflow = on_overflow

    def subscribe(self, session_id: str, maxsize: int) -> Subscription:
        sub = Subscription(session_id, maxsize)
        self._subs.setdefault(session_id, set()).add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        subs = self._subs.get(sub.session_id)
        if subs is not None:
            subs.discard(sub)
            if not subs:
                self._subs.pop(sub.session_id, None)

    def subscriber_count(self, session_id: str) -> int:
        return len(self._subs.get(session_id, ()))

    def publish(self, event: Event) -> None:
        for sub in list(self._subs.get(event.session_id, ())):
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                sub.overflowed = True
                self.unsubscribe(sub)
                if self._on_overflow is not None:
                    self._on_overflow()

    def close_session(self, session_id: str) -> None:
        """End every stream of a session (it was deleted)."""
        for sub in list(self._subs.get(session_id, ())):
            sub.closed = True
            # When full, the reader checks `closed` before it waits again.
            with contextlib.suppress(asyncio.QueueFull):
                sub.queue.put_nowait(None)
            self.unsubscribe(sub)
