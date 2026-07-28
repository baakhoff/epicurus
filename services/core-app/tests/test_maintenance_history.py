"""Unit tests for MaintenanceRunStore (#733): record/most_recent/page/prune.

File-backed SQLite (not `:memory:` on StaticPool) throughout, matching
test_automations_feed.py's `_engine` reasoning — nothing here touches the store from two
concurrent tasks the way the route tests do, but the convention is cheap to keep uniform.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from epicurus_core_app.maintenance import MaintenanceJobResult, MaintenanceRun
from epicurus_core_app.maintenance_history import MaintenanceRunStore

TENANT = "t1"


def _engine(tmp_path: Path, name: str = "history.db") -> AsyncEngine:
    return create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")


async def _store(tmp_path: Path, **kw: int) -> MaintenanceRunStore:
    store = MaintenanceRunStore(_engine(tmp_path), **kw)
    await store.init()
    return store


def _run(
    *,
    tenant: str = TENANT,
    scope: str = "all",
    source: str = "manual",
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    status: str = "ok",
) -> MaintenanceRun:
    started = started_at or datetime.now(UTC)
    finished = finished_at or started
    return MaintenanceRun(
        ran_at=started.isoformat(),
        scope=scope,  # type: ignore[arg-type]
        jobs=[MaintenanceJobResult(key="a", label="A", status=status, detail="done")],  # type: ignore[arg-type]
        tenant=tenant,
        source=source,  # type: ignore[arg-type]
        finished_at=finished.isoformat(),
    )


async def test_most_recent_on_a_fresh_tenant_is_none(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    assert await store.most_recent(TENANT) is None


async def test_record_then_most_recent_round_trips(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    await store.record(_run(scope="nightly", source="scheduled"))
    record = await store.most_recent(TENANT)
    assert record is not None
    assert record.tenant == TENANT
    assert record.scope == "nightly"
    assert record.source == "scheduled"
    assert record.jobs == [MaintenanceJobResult(key="a", label="A", status="ok", detail="done")]
    assert record.started_at and record.finished_at


async def test_most_recent_returns_the_newest_of_several(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    await store.record(_run(source="scheduled"))
    await store.record(_run(source="manual"))
    record = await store.most_recent(TENANT)
    assert record is not None
    assert record.source == "manual"


async def test_history_is_tenant_scoped(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    await store.record(_run(tenant="other"))
    assert await store.most_recent(TENANT) is None
    assert await store.most_recent("other") is not None


# ── page ─────────────────────────────────────────────────────────────────────────


async def test_page_on_a_fresh_tenant_is_empty(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    page = await store.page(TENANT, cursor=None, limit=10)
    assert page.runs == []
    assert page.next_cursor is None


async def test_page_is_newest_first_and_reports_no_more(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    for source in ("scheduled", "manual", "scheduled"):
        await store.record(_run(source=source))
    page = await store.page(TENANT, cursor=None, limit=10)
    assert [r.source for r in page.runs] == ["scheduled", "manual", "scheduled"]
    assert page.next_cursor is None


async def test_page_paginates_with_a_cursor(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    for i in range(5):
        await store.record(_run(source="scheduled" if i % 2 == 0 else "manual"))

    first = await store.page(TENANT, cursor=None, limit=2)
    assert len(first.runs) == 2
    assert first.next_cursor is not None

    second = await store.page(TENANT, cursor=first.next_cursor, limit=2)
    assert len(second.runs) == 2
    assert second.next_cursor is not None
    assert {r.id for r in first.runs} & {r.id for r in second.runs} == set()

    third = await store.page(TENANT, cursor=second.next_cursor, limit=2)
    assert len(third.runs) == 1
    assert third.next_cursor is None


# ── prune ────────────────────────────────────────────────────────────────────────


async def test_prune_on_an_empty_tenant_is_a_noop(tmp_path: Path) -> None:
    store = await _store(tmp_path, max_rows=200, max_age_days=90)
    assert await store.prune(TENANT) == 0


async def test_prune_caps_at_max_rows(tmp_path: Path) -> None:
    store = await _store(tmp_path, max_rows=3, max_age_days=9999)
    for _ in range(5):
        await store.record(_run())
    pruned = await store.prune(TENANT)
    assert pruned == 2
    page = await store.page(TENANT, cursor=None, limit=10)
    assert len(page.runs) == 3


async def test_prune_drops_rows_past_max_age(tmp_path: Path) -> None:
    store = await _store(tmp_path, max_rows=200, max_age_days=30)
    old = datetime.now(UTC) - timedelta(days=45)
    recent = datetime.now(UTC) - timedelta(days=1)
    # Distinguished by source, not by comparing timestamps after the round trip: SQLite hands
    # a `DateTime(timezone=True)` column back naive regardless of what was stored (the same
    # cross-database quirk `automations/runner.py`'s `_aware()` works around) — a string-equal
    # assertion against the tz-aware Python value would be comparing apples to oranges.
    await store.record(_run(started_at=old, finished_at=old, source="scheduled"))
    await store.record(_run(started_at=recent, finished_at=recent, source="manual"))
    pruned = await store.prune(TENANT)
    assert pruned == 1
    page = await store.page(TENANT, cursor=None, limit=10)
    assert len(page.runs) == 1
    assert page.runs[0].source == "manual"


async def test_prune_only_affects_its_own_tenant(tmp_path: Path) -> None:
    store = await _store(tmp_path, max_rows=1, max_age_days=9999)
    await store.record(_run(tenant="a"))
    await store.record(_run(tenant="a"))
    await store.record(_run(tenant="b"))
    await store.prune("a")
    assert len((await store.page("a", cursor=None, limit=10)).runs) == 1
    assert len((await store.page("b", cursor=None, limit=10)).runs) == 1  # untouched
