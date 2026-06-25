"""Unit tests for the nightly extraction runner (ADR-0051) — queue/extractor/power are faked."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from epicurus_core_app.memory.extraction import ExtractionRunner, _seconds_until_next_run
from epicurus_core_app.memory.extraction_queue import QueuedExchange


class _FakeQueue:
    """Holds a list of pending exchanges; records the ids handed to ``delete``."""

    def __init__(self, items: list[QueuedExchange]) -> None:
        self._items = list(items)
        self.deleted: list[int] = []

    async def pending(self, *, limit: int, tenant: str | None = None) -> list[QueuedExchange]:
        return self._items[:limit]

    async def delete(self, ids: list[int]) -> int:
        self.deleted.extend(ids)
        self._items = [item for item in self._items if item.id not in ids]
        return len(ids)


class _FakeExtractor:
    """Records each exchange it is asked to distil."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def extract(self, *, tenant: str, user_text: str, assistant_text: str) -> list[object]:
        self.calls.append((tenant, user_text, assistant_text))
        return []


class _FakePower:
    def __init__(self, paused: bool = False) -> None:
        self.paused = paused


async def _utc() -> str:
    return "UTC"


def _runner(queue: _FakeQueue, extractor: object, power: _FakePower) -> ExtractionRunner:
    return ExtractionRunner(queue, extractor, power, timezone=_utc)  # type: ignore[arg-type]


def _exchange(i: int, tenant: str = "t") -> QueuedExchange:
    return QueuedExchange(id=i, tenant=tenant, user_text=f"u{i}", assistant_text=f"a{i}")


async def test_drain_processes_and_deletes_each_exchange() -> None:
    queue = _FakeQueue([_exchange(1), _exchange(2)])
    extractor = _FakeExtractor()
    processed = await _runner(queue, extractor, _FakePower()).drain_once()
    assert processed == 2
    assert extractor.calls == [("t", "u1", "a1"), ("t", "u2", "a2")]
    assert queue.deleted == [1, 2]


async def test_drain_skips_entirely_when_paused() -> None:
    queue = _FakeQueue([_exchange(1)])
    extractor = _FakeExtractor()
    processed = await _runner(queue, extractor, _FakePower(paused=True)).drain_once()
    assert processed == 0
    assert extractor.calls == []
    assert queue.deleted == []  # nothing consumed — it waits for the next window


async def test_drain_stops_mid_batch_if_paused_under_it() -> None:
    queue = _FakeQueue([_exchange(1), _exchange(2), _exchange(3)])
    power = _FakePower()

    class _PauseAfterFirst:
        async def extract(
            self, *, tenant: str, user_text: str, assistant_text: str
        ) -> list[object]:
            power.paused = True  # the operator pauses the GPU while we drain
            return []

    processed = await _runner(queue, _PauseAfterFirst(), power).drain_once()
    assert processed == 1
    assert queue.deleted == [1]  # only the first consumed; 2 and 3 stay queued for next time


async def test_drain_deletes_even_when_an_extraction_raises() -> None:
    queue = _FakeQueue([_exchange(1)])

    class _BoomExtractor:
        async def extract(
            self, *, tenant: str, user_text: str, assistant_text: str
        ) -> list[object]:
            raise RuntimeError("model exploded")

    processed = await _runner(queue, _BoomExtractor(), _FakePower()).drain_once()
    assert processed == 1
    assert queue.deleted == [1]  # best-effort: a poison row must not wedge the queue forever


async def test_drain_respects_the_batch_limit() -> None:
    queue = _FakeQueue([_exchange(i) for i in range(5)])
    extractor = _FakeExtractor()
    processed = await _runner(queue, extractor, _FakePower()).drain_once(batch_limit=2)
    assert processed == 2


def test_seconds_until_next_run_is_later_today_before_the_hour() -> None:
    now = datetime(2026, 6, 25, 1, 0, tzinfo=ZoneInfo("UTC"))  # 1 AM, window at 3 AM
    assert _seconds_until_next_run(now, 3) == 2 * 3600


def test_seconds_until_next_run_rolls_to_tomorrow_past_the_hour() -> None:
    now = datetime(2026, 6, 25, 5, 0, tzinfo=ZoneInfo("UTC"))  # 5 AM, window already passed
    assert _seconds_until_next_run(now, 3) == 22 * 3600


def test_seconds_until_next_run_is_never_zero_at_exactly_the_hour() -> None:
    now = datetime(2026, 6, 25, 3, 0, tzinfo=ZoneInfo("UTC"))  # exactly 3 AM
    # Equal counts as "today's run is done" → schedule tomorrow, so the loop can't busy-spin.
    assert _seconds_until_next_run(now, 3) == 24 * 3600
