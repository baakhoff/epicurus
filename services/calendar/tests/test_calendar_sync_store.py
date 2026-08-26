"""Unit tests for calendar's reconcile-layer storage (#831).

Covers the two durable pieces the loop stands on: the per-collection sync cursor + observed
event cache (:class:`CalendarSyncStore`), and the self-write ledger
(:class:`SelfWriteLedger`). Tenant *and* collection scoping are asserted explicitly — a
reconcile layer that leaked either would announce one tenant's calendar into another's feed.

Every fixture is **file-backed** SQLite under ``tmp_path`` with default pooling, never
in-memory + ``StaticPool``: these stores are read and written from more than one task in the
suites that use them, and ``StaticPool`` shares a single DBAPI connection whose checkout-return
``ROLLBACK`` can land inside a concurrent writer's transaction. The engine is disposed in
teardown so aiosqlite's worker threads stop before the test's loop closes.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from epicurus_calendar.sync_store import CalendarSyncStore, SelfWriteLedger, SyncedEvent

TENANT = "local"
OTHER_TENANT = "other"


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'calendar-sync.db'}")
    yield engine
    await engine.dispose()


@pytest.fixture
async def store(engine: AsyncEngine) -> CalendarSyncStore:
    store = CalendarSyncStore(engine)
    await store.init()
    return store


def _dt(day: int, hour: int = 9) -> datetime:
    return datetime(2026, 6, day, hour, 0, 0, tzinfo=UTC)


def _observed(event_id: str, *, day: int = 15, series: str | None = None) -> SyncedEvent:
    return SyncedEvent(
        event_id=event_id,
        series_id=series,
        title=f"Event {event_id}",
        start=_dt(day),
        end=_dt(day, 10),
        change_hash="abc123",
    )


# ── sync cursor ──────────────────────────────────────────────────────────────


async def test_state_is_none_until_a_collection_has_ever_been_synced(
    store: CalendarSyncStore,
) -> None:
    """The load-bearing signal: ``None`` means "never synced", which is what keeps a first
    sync silent instead of announcing a calendar the operator already had."""
    assert await store.get_state(tenant=TENANT, account="google", collection="primary") is None


async def test_cursor_round_trips_and_stamps_when_it_was_advanced(
    store: CalendarSyncStore,
) -> None:
    anchor = _dt(1)
    await store.set_state(
        tenant=TENANT,
        account="google",
        collection="primary",
        sync_token="tok-1",
        window_start=anchor,
    )
    state = await store.get_state(tenant=TENANT, account="google", collection="primary")
    assert state is not None
    assert state.sync_token == "tok-1"
    assert state.window_start == anchor
    assert state.synced_at is not None


async def test_setting_the_cursor_again_replaces_rather_than_duplicates(
    store: CalendarSyncStore,
) -> None:
    for token in ("tok-1", "tok-2", "tok-3"):
        await store.set_state(
            tenant=TENANT,
            account="google",
            collection="primary",
            sync_token=token,
            window_start=None,
        )
    state = await store.get_state(tenant=TENANT, account="google", collection="primary")
    assert state is not None and state.sync_token == "tok-3"


async def test_a_primed_collection_with_no_cursor_is_still_primed(
    store: CalendarSyncStore,
) -> None:
    """``sync_token=None`` is "no resumable cursor", not "never synced" — the difference
    between diffing on the next pass and re-priming in silence."""
    await store.set_state(
        tenant=TENANT, account="google", collection="primary", sync_token=None, window_start=None
    )
    state = await store.get_state(tenant=TENANT, account="google", collection="primary")
    assert state is not None
    assert state.sync_token is None


async def test_cursors_are_scoped_per_tenant_and_per_collection(
    store: CalendarSyncStore,
) -> None:
    await store.set_state(
        tenant=TENANT, account="google", collection="a", sync_token="a1", window_start=None
    )
    await store.set_state(
        tenant=TENANT, account="google", collection="b", sync_token="b1", window_start=None
    )
    await store.set_state(
        tenant=OTHER_TENANT, account="google", collection="a", sync_token="z9", window_start=None
    )
    a = await store.get_state(tenant=TENANT, account="google", collection="a")
    b = await store.get_state(tenant=TENANT, account="google", collection="b")
    other = await store.get_state(tenant=OTHER_TENANT, account="google", collection="a")
    assert (a and a.sync_token) == "a1"
    assert (b and b.sync_token) == "b1"
    assert (other and other.sync_token) == "z9"


# ── observed-event cache ─────────────────────────────────────────────────────


async def test_upsert_then_read_back_one_observation(store: CalendarSyncStore) -> None:
    await store.upsert_events(
        tenant=TENANT, account="google", collection="primary", events=[_observed("e1")]
    )
    row = await store.get_event(
        tenant=TENANT, account="google", collection="primary", event_id="e1"
    )
    assert row is not None
    assert row.title == "Event e1"
    assert row.change_hash == "abc123"
    assert row.start == _dt(15)


async def test_upsert_replaces_an_existing_observation(store: CalendarSyncStore) -> None:
    await store.upsert_events(
        tenant=TENANT, account="google", collection="primary", events=[_observed("e1")]
    )
    moved = _observed("e1", day=16)
    moved.change_hash = "def456"
    await store.upsert_events(tenant=TENANT, account="google", collection="primary", events=[moved])
    rows = await store.get_events(tenant=TENANT, account="google", collection="primary")
    assert list(rows) == ["e1"]
    assert rows["e1"].change_hash == "def456"
    assert rows["e1"].start == _dt(16)


async def test_remove_events_drops_only_the_named_ids(store: CalendarSyncStore) -> None:
    await store.upsert_events(
        tenant=TENANT,
        account="google",
        collection="primary",
        events=[_observed("e1"), _observed("e2"), _observed("e3")],
    )
    await store.remove_events(
        tenant=TENANT, account="google", collection="primary", event_ids=["e2"]
    )
    rows = await store.get_events(tenant=TENANT, account="google", collection="primary")
    assert sorted(rows) == ["e1", "e3"]


async def test_replace_events_clears_the_collection_first(store: CalendarSyncStore) -> None:
    await store.upsert_events(
        tenant=TENANT,
        account="google",
        collection="primary",
        events=[_observed("old-1"), _observed("old-2")],
    )
    await store.replace_events(
        tenant=TENANT, account="google", collection="primary", events=[_observed("new-1")]
    )
    rows = await store.get_events(tenant=TENANT, account="google", collection="primary")
    assert list(rows) == ["new-1"]


async def test_the_cache_is_scoped_per_tenant_and_per_collection(
    store: CalendarSyncStore,
) -> None:
    await store.upsert_events(
        tenant=TENANT, account="google", collection="work", events=[_observed("e1")]
    )
    await store.upsert_events(
        tenant=OTHER_TENANT, account="google", collection="work", events=[_observed("e1")]
    )
    assert (
        await store.get_event(tenant=TENANT, account="google", collection="home", event_id="e1")
        is None
    )
    assert await store.count_events(tenant=TENANT) == 1
    assert await store.count_events(tenant=OTHER_TENANT) == 1


async def test_the_series_id_survives_a_round_trip(store: CalendarSyncStore) -> None:
    """A tombstone has no event object left, so the cached row is where the series id has to
    come from when a cancelled occurrence is collapsed onto its series."""
    await store.upsert_events(
        tenant=TENANT,
        account="google",
        collection="primary",
        events=[_observed("s1_20260615T090000Z", series="s1")],
    )
    row = await store.get_event(
        tenant=TENANT, account="google", collection="primary", event_id="s1_20260615T090000Z"
    )
    assert row is not None and row.series_id == "s1"


async def test_upserting_nothing_is_a_no_op(store: CalendarSyncStore) -> None:
    await store.upsert_events(tenant=TENANT, account="google", collection="p", events=[])
    await store.remove_events(tenant=TENANT, account="google", collection="p", event_ids=[])
    assert await store.count_events(tenant=TENANT) == 0


# ── self-write ledger ────────────────────────────────────────────────────────


@pytest.fixture
async def ledger(engine: AsyncEngine) -> SelfWriteLedger:
    ledger = SelfWriteLedger(engine, ttl_s=900.0)
    await ledger.init()
    return ledger


async def test_consume_reports_a_recorded_marker_once(ledger: SelfWriteLedger) -> None:
    """The exact-id path is *consumed*: a second, genuinely external change to the same event
    must be announced normally rather than swallowed by a marker that already did its job."""
    await ledger.record(tenant=TENANT, keys=["calendar.event_updated|google:e1"])
    assert await ledger.consume(tenant=TENANT, key="calendar.event_updated|google:e1") is True
    assert await ledger.consume(tenant=TENANT, key="calendar.event_updated|google:e1") is False


async def test_peek_leaves_the_marker_in_place(ledger: SelfWriteLedger) -> None:
    """The series path only peeks — one series-wide write legitimately matches many
    occurrences, and must keep matching until it expires."""
    await ledger.record(tenant=TENANT, keys=["calendar.event_created|google:series-1"])
    assert await ledger.peek(tenant=TENANT, key="calendar.event_created|google:series-1") is True
    assert await ledger.peek(tenant=TENANT, key="calendar.event_created|google:series-1") is True


async def test_an_unrecorded_key_is_never_suppressed(ledger: SelfWriteLedger) -> None:
    assert await ledger.consume(tenant=TENANT, key="calendar.event_created|google:nope") is False
    assert await ledger.peek(tenant=TENANT, key="calendar.event_created|google:nope") is False


async def test_markers_are_tenant_scoped(ledger: SelfWriteLedger) -> None:
    await ledger.record(tenant=TENANT, keys=["calendar.event_created|google:e1"])
    assert await ledger.peek(tenant=OTHER_TENANT, key="calendar.event_created|google:e1") is False


async def test_an_expired_marker_suppresses_nothing(engine: AsyncEngine) -> None:
    expired = SelfWriteLedger(engine, ttl_s=-1.0)
    await expired.init()
    await expired.record(tenant=TENANT, keys=["calendar.event_created|google:e1"])
    assert await expired.peek(tenant=TENANT, key="calendar.event_created|google:e1") is False
    assert await expired.consume(tenant=TENANT, key="calendar.event_created|google:e1") is False


async def test_prune_drops_expired_markers_and_keeps_live_ones(engine: AsyncEngine) -> None:
    live = SelfWriteLedger(engine, ttl_s=900.0)
    await live.init()
    stale = SelfWriteLedger(engine, ttl_s=-1.0)
    await live.record(tenant=TENANT, keys=["calendar.event_created|google:live"])
    await stale.record(tenant=TENANT, keys=["calendar.event_created|google:stale"])
    assert await live.prune(tenant=TENANT) == 1
    assert await live.peek(tenant=TENANT, key="calendar.event_created|google:live") is True


async def test_recording_a_key_again_refreshes_rather_than_duplicates(
    engine: AsyncEngine,
) -> None:
    stale = SelfWriteLedger(engine, ttl_s=-1.0)
    await stale.init()
    fresh = SelfWriteLedger(engine, ttl_s=900.0)
    key = "calendar.event_updated|google:e1"
    await stale.record(tenant=TENANT, keys=[key])
    await fresh.record(tenant=TENANT, keys=[key])
    assert await fresh.peek(tenant=TENANT, key=key) is True
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT count(*) FROM calendar_self_writes WHERE marker_key = :k"),
                {"k": key},
            )
        ).scalar_one()
    assert rows == 1


async def test_recording_no_keys_is_a_no_op(ledger: SelfWriteLedger) -> None:
    await ledger.record(tenant=TENANT, keys=[])
    assert await ledger.prune(tenant=TENANT) == 0


async def test_expiry_is_a_bigint_nanosecond_epoch(engine: AsyncEngine) -> None:
    """``expires_at_ns`` is ~1.8e18 — ``BigInteger``, never ``Integer``. SQLite tolerates the
    int32 overflow silently, so this asserts the stored value really is past 2**31 and the
    comparison that decides suppression still works on it (the knowledge-module mtime bug)."""
    ledger = SelfWriteLedger(engine, ttl_s=900.0)
    await ledger.init()
    await ledger.record(tenant=TENANT, keys=["calendar.event_created|google:e1"])
    async with engine.connect() as conn:
        stored = (
            await conn.execute(text("SELECT expires_at_ns FROM calendar_self_writes"))
        ).scalar_one()
    assert stored > 2**31
    assert stored > time.time_ns()
    assert stored < time.time_ns() + int(timedelta(hours=1).total_seconds() * 1e9)
    assert await ledger.peek(tenant=TENANT, key="calendar.event_created|google:e1") is True
