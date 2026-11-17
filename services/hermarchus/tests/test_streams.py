"""Bounded per-connection queues: a reader that falls behind is dropped with a resume
point, and never stalls the session or the other readers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from epicurus_hermarchus.app import stream_events
from epicurus_hermarchus.broker import Broker
from epicurus_hermarchus.kinds import load_kinds
from epicurus_hermarchus.metrics import Metrics
from epicurus_hermarchus.runtime import SessionManager
from epicurus_hermarchus.store import Event, Store


def _ev(seq: int) -> Event:
    return Event(session_id="s", seq=seq, type="t", at="now", data={})


def test_a_full_queue_drops_only_that_subscriber() -> None:
    overflows: list[int] = []
    broker = Broker(on_overflow=lambda: overflows.append(1))
    slow = broker.subscribe("s", 2)
    fast = broker.subscribe("s", 100)
    for seq in range(1, 6):
        broker.publish(_ev(seq))
    assert slow.overflowed and not fast.overflowed
    assert slow.queue.qsize() == 2
    assert fast.queue.qsize() == 5
    assert broker.subscriber_count("s") == 1
    assert overflows == [1]


def test_closing_a_session_ends_its_streams_even_when_full() -> None:
    broker = Broker()
    sub = broker.subscribe("s", 1)
    broker.publish(_ev(1))
    broker.close_session("s")
    assert sub.closed and broker.subscriber_count("s") == 0


async def test_a_reader_that_falls_behind_gets_an_overflow_frame(
    settings_for: Any, tmp_path: Path
) -> None:
    settings = settings_for()
    store = await Store.open(settings.database_path, "local")
    broker = Broker()
    mgr = SessionManager(
        settings=settings, store=store, broker=broker, kinds=load_kinds(), metrics=Metrics()
    )
    try:
        row = await store.create_session(agent="echo", cwd=".", title=None, unattended=False)
        sid = row["id"]
        mgr.live[sid] = object()  # type: ignore[assignment]  # "live" is all the stream asks
        await mgr.record(sid, "first", {})
        slow = stream_events(mgr, broker, sid, after=0, follow=True, queue_size=2)
        fast = stream_events(mgr, broker, sid, after=0, follow=True, queue_size=100)
        assert b"event: first" in await slow.__anext__()
        assert b"event: first" in await fast.__anext__()
        for i in range(5):
            await mgr.record(sid, f"e{i}", {})  # never blocks on the slow reader
        frame = (await slow.__anext__()).decode()
        assert frame.startswith("event: stream.overflow")
        payload = json.loads(frame.split("data: ", 1)[1])
        assert payload == {"resume_after": 1}
        assert [frame async for frame in slow] == []  # and the stream has ended
        got = [(await fast.__anext__()).decode().split("\n")[1] for _ in range(5)]
        assert got == [f"event: e{i}" for i in range(5)]
        # Resuming from the overflow point replays exactly what was missed.
        resumed = stream_events(mgr, broker, sid, after=1, follow=False, queue_size=2)
        frames = [f.decode() async for f in resumed]
        assert [f.split("\n")[0] for f in frames] == [f"id: {n}" for n in range(2, 7)]
        await fast.aclose()
    finally:
        await store.close()
