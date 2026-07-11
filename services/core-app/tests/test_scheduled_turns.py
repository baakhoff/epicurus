"""Tests for scheduled turns (ADR-0092): the store, the due-ness/tick logic, and the routes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_core import ChatMessage
from epicurus_core_app.scheduled_turns import (
    ScheduledTurn,
    ScheduledTurnScheduler,
    ScheduledTurnStore,
    _is_due,
    validate_cadence,
)
from epicurus_core_app.scheduled_turns_routes import create_scheduled_turns_router

TENANT = "test"


# ── fakes ────────────────────────────────────────────────────────────────────


class _FakePower:
    def __init__(self, paused: bool = False) -> None:
        self.paused = paused


class _FakeAgent:
    """Records each headless turn it is asked to run; can be made to fail."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[list[ChatMessage], str | None, str | None]] = []
        self._fail = fail

    async def run(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        tenant_id: str | None = None,
        session_id: str | None = None,
    ) -> Any:
        self.calls.append((messages, tenant_id, session_id))
        if self._fail:
            raise RuntimeError("gateway exploded")
        return object()


async def _utc() -> str:
    return "UTC"


async def _store() -> ScheduledTurnStore:
    store = ScheduledTurnStore(create_async_engine("sqlite+aiosqlite:///:memory:"))
    await store.init()
    return store


def _scheduler(
    store: ScheduledTurnStore, agent: _FakeAgent, power: _FakePower
) -> ScheduledTurnScheduler:
    return ScheduledTurnScheduler(store, agent, power, timezone=_utc)  # type: ignore[arg-type]


# ── validate_cadence ───────────────────────────────────────────────────────────


def test_validate_cadence_accepts_daily_without_weekday() -> None:
    validate_cadence("daily", None)  # no raise


@pytest.mark.parametrize("weekday", [0, 3, 6])
def test_validate_cadence_accepts_weekly_with_a_valid_weekday(weekday: int) -> None:
    validate_cadence("weekly", weekday)  # no raise


def test_validate_cadence_rejects_weekly_without_weekday() -> None:
    with pytest.raises(ValueError, match="weekday"):
        validate_cadence("weekly", None)


@pytest.mark.parametrize("weekday", [-1, 7, 100])
def test_validate_cadence_rejects_an_out_of_range_weekday(weekday: int) -> None:
    with pytest.raises(ValueError, match="weekday"):
        validate_cadence("weekly", weekday)


def test_validate_cadence_rejects_unknown_cadence() -> None:
    with pytest.raises(ValueError, match="cadence"):
        validate_cadence("hourly", None)


# ── ScheduledTurnStore ─────────────────────────────────────────────────────────


async def test_create_returns_an_enabled_row() -> None:
    store = await _store()
    turn = await store.create(
        tenant=TENANT,
        prompt="Summarize my day",
        cadence="daily",
        hour=7,
        weekday=None,
        delivery_target="scheduled-abc",
    )
    assert turn.id and len(turn.id) == 32  # uuid4 hex
    assert turn.enabled is True
    assert turn.hour == 7
    assert turn.last_run_at is None
    assert turn.last_status is None


async def test_create_normalises_an_out_of_range_hour() -> None:
    store = await _store()
    turn = await store.create(
        tenant=TENANT,
        prompt="p",
        cadence="daily",
        hour=25,
        weekday=None,
        delivery_target="scheduled-a",
    )
    assert turn.hour == 1  # 25 % 24


async def test_list_is_tenant_scoped_and_ordered() -> None:
    store = await _store()
    await store.create(
        tenant=TENANT,
        prompt="first",
        cadence="daily",
        hour=7,
        weekday=None,
        delivery_target="scheduled-1",
    )
    await store.create(
        tenant=TENANT,
        prompt="second",
        cadence="daily",
        hour=8,
        weekday=None,
        delivery_target="scheduled-2",
    )
    await store.create(
        tenant="other",
        prompt="third",
        cadence="daily",
        hour=9,
        weekday=None,
        delivery_target="scheduled-3",
    )
    rows = await store.list(tenant=TENANT)
    assert [r.prompt for r in rows] == ["first", "second"]
    assert len(await store.list(tenant="other")) == 1


async def test_list_enabled_spans_every_tenant_and_excludes_disabled() -> None:
    store = await _store()
    a = await store.create(
        tenant="t1",
        prompt="a",
        cadence="daily",
        hour=7,
        weekday=None,
        delivery_target="scheduled-a",
    )
    await store.create(
        tenant="t2",
        prompt="b",
        cadence="daily",
        hour=7,
        weekday=None,
        delivery_target="scheduled-b",
    )
    await store.set_enabled(tenant="t1", turn_id=a.id, enabled=False)
    enabled = await store.list_enabled()
    assert [r.prompt for r in enabled] == ["b"]


async def test_get_returns_none_for_unknown_or_wrong_tenant() -> None:
    store = await _store()
    turn = await store.create(
        tenant=TENANT,
        prompt="p",
        cadence="daily",
        hour=7,
        weekday=None,
        delivery_target="scheduled-a",
    )
    assert await store.get(tenant=TENANT, turn_id=turn.id) is not None
    assert await store.get(tenant=TENANT, turn_id="nope") is None
    assert await store.get(tenant="other", turn_id=turn.id) is None  # wrong tenant


async def test_set_enabled_toggles_and_reports_unknown() -> None:
    store = await _store()
    turn = await store.create(
        tenant=TENANT,
        prompt="p",
        cadence="daily",
        hour=7,
        weekday=None,
        delivery_target="scheduled-a",
    )
    assert await store.set_enabled(tenant=TENANT, turn_id=turn.id, enabled=False) is True
    got = await store.get(tenant=TENANT, turn_id=turn.id)
    assert got is not None and got.enabled is False
    assert await store.set_enabled(tenant=TENANT, turn_id="nope", enabled=True) is False


async def test_delete_removes_and_reports_unknown() -> None:
    store = await _store()
    turn = await store.create(
        tenant=TENANT,
        prompt="p",
        cadence="daily",
        hour=7,
        weekday=None,
        delivery_target="scheduled-a",
    )
    assert await store.delete(tenant=TENANT, turn_id=turn.id) is True
    assert await store.get(tenant=TENANT, turn_id=turn.id) is None
    assert await store.delete(tenant=TENANT, turn_id=turn.id) is False  # already gone


async def test_mark_run_records_status_and_timestamp() -> None:
    store = await _store()
    turn = await store.create(
        tenant=TENANT,
        prompt="p",
        cadence="daily",
        hour=7,
        weekday=None,
        delivery_target="scheduled-a",
    )
    ran_at = datetime(2026, 7, 12, 7, 0, tzinfo=UTC)
    await store.mark_run(turn_id=turn.id, status="ok", ran_at=ran_at)
    got = await store.get(tenant=TENANT, turn_id=turn.id)
    assert got is not None
    assert got.last_status == "ok"
    # SQLite (unlike Postgres) drops tzinfo on a DateTime(timezone=True) round-trip — compare
    # naively rather than asserting exact datetime equality including tzinfo.
    assert got.last_run_at is not None
    assert got.last_run_at.replace(tzinfo=None) == ran_at.replace(tzinfo=None)


async def test_mark_run_on_a_deleted_row_is_a_silent_no_op() -> None:
    store = await _store()
    turn = await store.create(
        tenant=TENANT,
        prompt="p",
        cadence="daily",
        hour=7,
        weekday=None,
        delivery_target="scheduled-a",
    )
    await store.delete(tenant=TENANT, turn_id=turn.id)
    await store.mark_run(turn_id=turn.id, status="ok", ran_at=datetime.now(UTC))  # no raise


# ── _is_due ──────────────────────────────────────────────────────────────────


def _turn(**overrides: object) -> ScheduledTurn:
    defaults: dict[str, object] = {
        "id": "t1",
        "tenant": TENANT,
        "prompt": "p",
        "cadence": "daily",
        "hour": 7,
        "weekday": None,
        "delivery_target": "scheduled-a",
        "enabled": True,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "last_run_at": None,
        "last_status": None,
    }
    defaults.update(overrides)
    return ScheduledTurn(**defaults)  # type: ignore[arg-type]


def test_daily_is_due_at_the_matching_hour_when_never_run() -> None:
    now = datetime(2026, 7, 12, 7, 30, tzinfo=UTC)
    assert _is_due(_turn(hour=7), now) is True


def test_not_due_at_a_different_hour() -> None:
    now = datetime(2026, 7, 12, 8, 0, tzinfo=UTC)
    assert _is_due(_turn(hour=7), now) is False


def test_weekly_not_due_on_the_wrong_weekday() -> None:
    now = datetime(2026, 7, 12, 7, 30, tzinfo=UTC)  # a Sunday (weekday() == 6)
    assert _is_due(_turn(cadence="weekly", hour=7, weekday=0), now) is False  # wants Monday


def test_weekly_is_due_on_the_matching_weekday_and_hour() -> None:
    now = datetime(2026, 7, 13, 7, 30, tzinfo=UTC)  # a Monday
    assert _is_due(_turn(cadence="weekly", hour=7, weekday=0), now) is True


def test_not_due_again_the_same_day_it_already_ran() -> None:
    now = datetime(2026, 7, 12, 7, 45, tzinfo=UTC)
    already_ran = datetime(2026, 7, 12, 7, 1, tzinfo=UTC)
    assert _is_due(_turn(hour=7, last_run_at=already_ran), now) is False


def test_due_again_the_next_day() -> None:
    now = datetime(2026, 7, 13, 7, 0, tzinfo=UTC)
    ran_yesterday = datetime(2026, 7, 12, 7, 1, tzinfo=UTC)
    assert _is_due(_turn(hour=7, last_run_at=ran_yesterday), now) is True


# ── ScheduledTurnScheduler.tick ─────────────────────────────────────────────────


async def test_run_one_delivers_the_prompt_through_the_agent() -> None:
    store = await _store()
    await store.create(
        tenant=TENANT,
        prompt="Summarize my day",
        cadence="daily",
        hour=7,
        weekday=None,
        delivery_target="scheduled-a",
    )
    agent = _FakeAgent()
    scheduler = _scheduler(store, agent, _FakePower())
    turn = (await store.list(tenant=TENANT))[0]
    await scheduler._run_one(turn)
    assert len(agent.calls) == 1
    messages, tenant_id, session_id = agent.calls[0]
    assert messages == [ChatMessage(role="user", content="Summarize my day")]
    assert tenant_id == TENANT
    assert session_id == "scheduled-a"
    got = await store.get(tenant=TENANT, turn_id=turn.id)
    assert got is not None and got.last_status == "ok" and got.last_run_at is not None


async def test_run_one_skips_and_records_when_paused() -> None:
    store = await _store()
    await store.create(
        tenant=TENANT,
        prompt="p",
        cadence="daily",
        hour=7,
        weekday=None,
        delivery_target="scheduled-a",
    )
    agent = _FakeAgent()
    scheduler = _scheduler(store, agent, _FakePower(paused=True))
    turn = (await store.list(tenant=TENANT))[0]
    await scheduler._run_one(turn)
    assert agent.calls == []  # never called the gateway while paused
    got = await store.get(tenant=TENANT, turn_id=turn.id)
    assert got is not None
    assert got.last_status == "skipped (paused)"
    assert got.last_run_at is not None  # recorded, so the same window isn't re-evaluated


async def test_run_one_records_an_error_status_without_raising() -> None:
    store = await _store()
    await store.create(
        tenant=TENANT,
        prompt="p",
        cadence="daily",
        hour=7,
        weekday=None,
        delivery_target="scheduled-a",
    )
    agent = _FakeAgent(fail=True)
    scheduler = _scheduler(store, agent, _FakePower())
    turn = (await store.list(tenant=TENANT))[0]
    await scheduler._run_one(turn)  # must not raise — one row's failure can't break the tick
    got = await store.get(tenant=TENANT, turn_id=turn.id)
    assert got is not None
    assert got.last_status is not None and got.last_status.startswith("error:")


async def test_tick_only_runs_rows_due_at_the_current_hour() -> None:
    store = await _store()
    await store.create(
        tenant=TENANT,
        prompt="due",
        cadence="daily",
        hour=datetime.now(UTC).hour,
        weekday=None,
        delivery_target="scheduled-due",
    )
    await store.create(
        tenant=TENANT,
        prompt="not due",
        cadence="daily",
        hour=(datetime.now(UTC).hour + 5) % 24,
        weekday=None,
        delivery_target="scheduled-not-due",
    )
    agent = _FakeAgent()
    scheduler = _scheduler(store, agent, _FakePower())
    await scheduler.tick()
    assert len(agent.calls) == 1
    assert agent.calls[0][2] == "scheduled-due"


async def test_tick_never_raises_on_a_bad_timezone() -> None:
    store = await _store()
    await store.create(
        tenant=TENANT,
        prompt="p",
        cadence="daily",
        hour=0,
        weekday=None,
        delivery_target="scheduled-a",
    )

    async def _bad_tz() -> str:
        return "Not/AZone"

    scheduler = ScheduledTurnScheduler(store, _FakeAgent(), _FakePower(), timezone=_bad_tz)  # type: ignore[arg-type]
    await scheduler.tick()  # falls back to UTC rather than raising


# ── HTTP routes ────────────────────────────────────────────────────────────────


def _app(store: ScheduledTurnStore) -> TestClient:
    app = FastAPI()
    app.include_router(create_scheduled_turns_router(store, default_tenant=TENANT))
    return TestClient(app, raise_server_exceptions=True)


async def test_create_endpoint_stages_a_daily_turn() -> None:
    store = await _store()
    client = _app(store)
    resp = client.post(
        "/platform/v1/scheduled-turns",
        json={"prompt": "Morning briefing", "cadence": "daily", "hour": 7},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["prompt"] == "Morning briefing"
    assert body["enabled"] is True
    assert body["delivery_target"].startswith("scheduled-")
    rows = await store.list(tenant=TENANT)
    assert len(rows) == 1


async def test_create_endpoint_rejects_a_blank_prompt() -> None:
    store = await _store()
    client = _app(store)
    resp = client.post(
        "/platform/v1/scheduled-turns", json={"prompt": "   ", "cadence": "daily", "hour": 7}
    )
    assert resp.status_code == 400


async def test_create_endpoint_rejects_an_out_of_range_hour() -> None:
    store = await _store()
    client = _app(store)
    resp = client.post(
        "/platform/v1/scheduled-turns", json={"prompt": "p", "cadence": "daily", "hour": 24}
    )
    assert resp.status_code == 400


async def test_create_endpoint_rejects_weekly_without_a_weekday() -> None:
    store = await _store()
    client = _app(store)
    resp = client.post(
        "/platform/v1/scheduled-turns", json={"prompt": "p", "cadence": "weekly", "hour": 7}
    )
    assert resp.status_code == 400


async def test_list_endpoint_is_tenant_scoped() -> None:
    store = await _store()
    await store.create(
        tenant=TENANT,
        prompt="mine",
        cadence="daily",
        hour=7,
        weekday=None,
        delivery_target="scheduled-a",
    )
    await store.create(
        tenant="other",
        prompt="not mine",
        cadence="daily",
        hour=7,
        weekday=None,
        delivery_target="scheduled-b",
    )
    client = _app(store)
    resp = client.get("/platform/v1/scheduled-turns")
    assert resp.status_code == 200
    assert [t["prompt"] for t in resp.json()] == ["mine"]


async def test_enabled_endpoint_toggles_and_404s_unknown() -> None:
    store = await _store()
    turn = await store.create(
        tenant=TENANT,
        prompt="p",
        cadence="daily",
        hour=7,
        weekday=None,
        delivery_target="scheduled-a",
    )
    client = _app(store)
    resp = client.post(f"/platform/v1/scheduled-turns/{turn.id}/enabled", json={"enabled": False})
    assert resp.status_code == 200
    got = await store.get(tenant=TENANT, turn_id=turn.id)
    assert got is not None and got.enabled is False

    resp = client.post("/platform/v1/scheduled-turns/nope/enabled", json={"enabled": True})
    assert resp.status_code == 404


async def test_delete_endpoint_removes_and_404s_unknown() -> None:
    store = await _store()
    turn = await store.create(
        tenant=TENANT,
        prompt="p",
        cadence="daily",
        hour=7,
        weekday=None,
        delivery_target="scheduled-a",
    )
    client = _app(store)
    resp = client.delete(f"/platform/v1/scheduled-turns/{turn.id}")
    assert resp.status_code == 204
    assert await store.get(tenant=TENANT, turn_id=turn.id) is None

    resp = client.delete(f"/platform/v1/scheduled-turns/{turn.id}")
    assert resp.status_code == 404
