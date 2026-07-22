"""Unit tests for the resilient background index runner (#230)."""

from __future__ import annotations

import asyncio

import pytest

from epicurus_knowledge.runner import IndexRunner


class _FakeIndexer:
    """A source indexer whose ``run`` returns fixed counts or raises a set number of times."""

    def __init__(
        self,
        counts: dict[str, int] | None = None,
        *,
        fail_times: int = 0,
        exc: Exception | None = None,
        reconcile_returns: bool = False,
        order_log: list[str] | None = None,
        name: str = "fake",
    ) -> None:
        self._counts = counts or {"indexed": 0, "deleted": 0, "unchanged": 0}
        self._fail_times = fail_times
        self._exc = exc or RuntimeError("dep not ready")
        self._reconcile_returns = reconcile_returns
        self._order_log = order_log
        self._name = name
        self.calls = 0
        self.reconciled = 0

    async def reconcile(self) -> bool:
        self.reconciled += 1
        if self._order_log is not None:
            self._order_log.append(f"reconcile:{self._name}")
        return self._reconcile_returns

    async def run(self) -> dict[str, int]:
        self.calls += 1
        if self._order_log is not None:
            self._order_log.append(f"run:{self._name}")
        if self.calls <= self._fail_times:
            raise self._exc
        return dict(self._counts)


async def test_run_once_sums_counts_across_indexers() -> None:
    runner = IndexRunner(
        [
            _FakeIndexer({"indexed": 2, "deleted": 1, "unchanged": 3}),
            _FakeIndexer({"indexed": 5, "deleted": 0, "unchanged": 1}),
        ]
    )
    total = await runner.run_once()
    assert total == {"indexed": 7, "deleted": 1, "unchanged": 4}


async def test_run_with_retry_succeeds_first_try() -> None:
    indexer = _FakeIndexer({"indexed": 4, "deleted": 0, "unchanged": 0})
    runner = IndexRunner([indexer])
    await runner.run_with_retry()
    assert runner.state.phase == "ready"
    assert runner.state.attempts == 1
    assert runner.state.error is None
    assert runner.state.last_result == {"indexed": 4, "deleted": 0, "unchanged": 0}
    assert indexer.calls == 1


async def test_run_with_retry_recovers_after_failures() -> None:
    # Fails twice (deps not ready), succeeds on the third attempt.
    indexer = _FakeIndexer({"indexed": 1, "deleted": 0, "unchanged": 0}, fail_times=2)
    runner = IndexRunner([indexer], base_delay_seconds=0.0, max_delay_seconds=0.0)
    await runner.run_with_retry()
    assert runner.state.phase == "ready"
    assert runner.state.attempts == 3
    assert runner.state.last_result == {"indexed": 1, "deleted": 0, "unchanged": 0}


async def test_run_with_retry_gives_up_after_max_attempts() -> None:
    indexer = _FakeIndexer(fail_times=99, exc=RuntimeError("connection refused"))
    runner = IndexRunner([indexer], max_attempts=3, base_delay_seconds=0.0, max_delay_seconds=0.0)
    await runner.run_with_retry()
    assert runner.state.phase == "error"
    assert runner.state.attempts == 3
    assert runner.state.error == "connection refused"
    assert indexer.calls == 3


async def test_on_complete_called_with_totals() -> None:
    seen: list[dict[str, int]] = []

    async def _capture(total: dict[str, int]) -> None:
        seen.append(total)

    runner = IndexRunner(
        [_FakeIndexer({"indexed": 2, "deleted": 0, "unchanged": 0})], on_complete=_capture
    )
    await runner.run_with_retry()
    assert seen == [{"indexed": 2, "deleted": 0, "unchanged": 0}]


async def test_on_complete_failure_does_not_fail_run() -> None:
    async def _boom(_: dict[str, int]) -> None:
        raise RuntimeError("event bus down")

    runner = IndexRunner([_FakeIndexer()], on_complete=_boom)
    await runner.run_with_retry()  # must not raise
    assert runner.state.phase == "ready"


async def test_on_failed_called_once_when_retries_are_exhausted() -> None:
    """The spine hook (#665): the terminal give-up reports its last error, once."""
    failures: list[str] = []

    async def _capture(error: str) -> None:
        failures.append(error)

    indexer = _FakeIndexer(fail_times=99, exc=RuntimeError("connection refused"))
    runner = IndexRunner(
        [indexer],
        max_attempts=3,
        base_delay_seconds=0.0,
        max_delay_seconds=0.0,
        on_failed=_capture,
    )
    await runner.run_with_retry()
    assert failures == ["connection refused"]
    assert runner.state.phase == "error"


async def test_on_failed_not_called_on_eventual_success() -> None:
    failures: list[str] = []

    async def _capture(error: str) -> None:
        failures.append(error)

    indexer = _FakeIndexer(fail_times=1)
    runner = IndexRunner(
        [indexer], max_attempts=3, base_delay_seconds=0.0, max_delay_seconds=0.0, on_failed=_capture
    )
    await runner.run_with_retry()
    assert failures == []
    assert runner.state.phase == "ready"


async def test_on_failed_failure_does_not_mask_the_give_up() -> None:
    async def _boom(_: str) -> None:
        raise RuntimeError("event bus down")

    indexer = _FakeIndexer(fail_times=99, exc=RuntimeError("connection refused"))
    runner = IndexRunner(
        [indexer], max_attempts=2, base_delay_seconds=0.0, max_delay_seconds=0.0, on_failed=_boom
    )
    await runner.run_with_retry()  # must not raise
    assert runner.state.phase == "error"


async def test_backoff_is_capped_exponential() -> None:
    runner = IndexRunner([_FakeIndexer()], base_delay_seconds=1.0, max_delay_seconds=10.0)
    assert runner._backoff(1) == 1.0
    assert runner._backoff(2) == 2.0
    assert runner._backoff(3) == 4.0
    assert runner._backoff(4) == 8.0
    assert runner._backoff(5) == 10.0  # capped
    assert runner._backoff(6) == 10.0


async def test_snapshot_reports_state() -> None:
    runner = IndexRunner([_FakeIndexer({"indexed": 1, "deleted": 0, "unchanged": 0})])
    await runner.run_with_retry()
    snap = runner.state.snapshot()
    assert snap["phase"] == "ready"
    assert snap["attempts"] == 1
    assert snap["last_result"] == {"indexed": 1, "deleted": 0, "unchanged": 0}


async def test_cancellation_propagates() -> None:
    started = asyncio.Event()

    class _Hang:
        async def reconcile(self) -> bool:
            return False

        async def run(self) -> dict[str, int]:
            started.set()
            await asyncio.sleep(3600)
            return {"indexed": 0, "deleted": 0, "unchanged": 0}

    runner = IndexRunner([_Hang()])
    task = asyncio.create_task(runner.run_with_retry())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ── Reconcile pre-pass (#229) ─────────────────────────────────────────────────


async def test_reconcile_runs_for_every_source_before_any_run() -> None:
    """All sources reconcile up front, before the first run recreates a collection."""
    order: list[str] = []
    a = _FakeIndexer(order_log=order, name="a")
    b = _FakeIndexer(order_log=order, name="b")
    runner = IndexRunner([a, b])
    await runner.run_once()
    # Every reconcile precedes every run — critical for sources sharing a collection.
    assert order == ["reconcile:a", "reconcile:b", "run:a", "run:b"]
    assert a.reconciled == 1
    assert b.reconciled == 1


async def test_run_with_retry_reconciles_each_attempt() -> None:
    indexer = _FakeIndexer({"indexed": 1, "deleted": 0, "unchanged": 0}, fail_times=1)
    runner = IndexRunner([indexer], base_delay_seconds=0.0, max_delay_seconds=0.0)
    await runner.run_with_retry()
    # Reconcile is idempotent, so running it on each attempt (here, twice) is safe.
    assert indexer.reconciled == 2
    assert runner.state.phase == "ready"
