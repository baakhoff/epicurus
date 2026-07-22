"""The notes spine emitter (#665): debounce-to-settled, immediate create/delete, re-key.

The fake bus replaces only the wire — every test still goes through the real
``emit_event``, so envelope validation (module-prefixed type, payload cap, the
credential-shaped-key screen, tz-aware timestamps) is exercised on every emission.

Due-ness is driven through :meth:`flush_due` with a hand-rolled clock — no sleeping,
the ADR-0092/0098 test idiom.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from epicurus_notes.events import NoteEventEmitter

TENANT = "test"


class _FakeBus:
    """Records spine publishes — the repo-standard EventBus stand-in."""

    def __init__(self, *, fail: bool = False) -> None:
        self.published: list[tuple[str, dict[str, Any], str | None]] = []
        self._fail = fail

    async def publish(
        self, subject: str, data: dict[str, Any], tenant_id: str | None = None
    ) -> None:
        if self._fail:
            raise RuntimeError("nats is down")
        self.published.append((subject, data, tenant_id))


class _Clock:
    """A monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _emitter(bus: _FakeBus, clock: _Clock, *, debounce_s: float = 120.0) -> NoteEventEmitter:
    return NoteEventEmitter(bus, tenant=TENANT, debounce_s=debounce_s, clock=clock)  # type: ignore[arg-type]


async def test_rapid_saves_settle_into_exactly_one_update() -> None:
    """The #665 acceptance: a burst of auto-saves is one event, after the quiet window."""
    bus, clock = _FakeBus(), _Clock()
    emitter = _emitter(bus, clock)

    for _ in range(5):  # an editing session: five idle-saves in quick succession
        await emitter.note_saved("daily/log", "Log", created=False)
        clock.now += 3.0

    assert await emitter.flush_due() == 0  # still inside the quiet window
    assert bus.published == []

    clock.now += 120.0
    assert await emitter.flush_due() == 1
    [(subject, data, tenant)] = bus.published
    assert subject == "events.notes.note_updated"
    assert tenant == TENANT
    assert data["payload"] == {"slug": "daily/log", "title": "Log", "saves": 5}
    # The event reports the change, not the sweep: occurred_at is the last save's time,
    # and the dedup key is derived from that same instant (deterministic per settle).
    key_instant = datetime.fromisoformat(data["dedup_key"].removeprefix("daily/log:updated:"))
    assert datetime.fromisoformat(data["occurred_at"]) == key_instant

    # Settled means settled — a later sweep emits nothing more.
    clock.now += 500.0
    assert await emitter.flush_due() == 0


async def test_new_save_rearms_the_quiet_window() -> None:
    bus, clock = _FakeBus(), _Clock()
    emitter = _emitter(bus, clock, debounce_s=100.0)

    await emitter.note_saved("a", "A", created=False)
    clock.now += 90.0
    await emitter.note_saved("a", "A", created=False)  # re-arms: deadline moves out
    clock.now += 90.0  # 180 past the first save, only 90 past the second
    assert await emitter.flush_due() == 0
    clock.now += 10.1
    assert await emitter.flush_due() == 1


async def test_created_emits_immediately_and_carries_the_title() -> None:
    bus, clock = _FakeBus(), _Clock()
    emitter = _emitter(bus, clock)

    await emitter.note_saved("fresh", "Fresh note", created=True)

    [(subject, data, _)] = bus.published
    assert subject == "events.notes.note_created"
    assert data["payload"] == {"slug": "fresh", "title": "Fresh note"}
    assert data["entity_ref"]["module"] == "notes"
    assert data["entity_ref"]["kind"] == "note"


async def test_delete_drops_the_pending_update() -> None:
    bus, clock = _FakeBus(), _Clock()
    emitter = _emitter(bus, clock)

    await emitter.note_saved("doomed", "Doomed", created=False)
    await emitter.note_deleted("doomed")
    clock.now += 1000.0

    assert await emitter.flush_due() == 0  # the pending update died with the note
    [(subject, data, _)] = bus.published
    assert subject == "events.notes.note_deleted"
    assert data["payload"] == {"slug": "doomed"}


async def test_move_rekeys_the_pending_update() -> None:
    bus, clock = _FakeBus(), _Clock()
    emitter = _emitter(bus, clock)

    await emitter.note_saved("old-name", "Body", created=False)
    emitter.note_moved("old-name", "new-name")
    clock.now += 1000.0

    assert await emitter.flush_due() == 1
    [(subject, data, _)] = bus.published
    assert subject == "events.notes.note_updated"
    assert data["payload"]["slug"] == "new-name"  # settles under the slug that exists


async def test_flush_all_emits_pending_immediately() -> None:
    """The shutdown path: a restart must not swallow an unswept editing session."""
    bus, clock = _FakeBus(), _Clock()
    emitter = _emitter(bus, clock)

    await emitter.note_saved("a", "A", created=False)
    await emitter.note_saved("b", "B", created=False)

    assert await emitter.flush_all() == 2
    assert {s for s, _, _ in bus.published} == {"events.notes.note_updated"}
    assert await emitter.flush_all() == 0  # drained


async def test_bus_failure_never_raises() -> None:
    bus, clock = _FakeBus(fail=True), _Clock()
    emitter = _emitter(bus, clock)

    await emitter.note_saved("a", "A", created=True)  # immediate emit path
    await emitter.note_saved("a", "A", created=False)
    clock.now += 1000.0
    assert await emitter.flush_due() == 1  # swept without raising; emit failed quietly


async def test_no_bus_disables_emission() -> None:
    clock = _Clock()
    emitter = NoteEventEmitter(None, tenant=TENANT, clock=clock)
    await emitter.note_saved("a", "A", created=True)
    await emitter.note_saved("a", "A", created=False)
    clock.now += 1000.0
    assert await emitter.flush_due() == 1  # the sweep still drains its book-keeping


async def test_payload_is_wire_serializable_and_pointer_sized() -> None:
    """The envelope's own validators ran (real emit_event); spot-check the wire shape."""
    bus, clock = _FakeBus(), _Clock()
    emitter = _emitter(bus, clock)
    await emitter.note_saved("n", "T" * 500, created=True)  # long title is capped
    [(_, data, _)] = bus.published
    assert len(data["payload"]["title"]) == 200
    assert len(json.dumps(data["payload"]).encode()) < 4096
