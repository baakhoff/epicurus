"""Tests for the automations runner, store, sinks, and the scheduled-turns fold-in.

The safety properties are the point of this file: the loop guard, the kill switch, the
rate cap, and the digest window each get a test that proves the *stop*, not the happy path.
A safety feature that is only tested when it doesn't fire isn't tested.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core import ChatMessage, EntityRef
from epicurus_core_app.agent.agent import AgentTurn, TurnUsage
from epicurus_core_app.automations.migration import MIGRATED_MARKER, migrate_scheduled_turns
from epicurus_core_app.automations.model import (
    Automation,
    EventTrigger,
    PayloadMatcher,
    ScheduleTrigger,
)
from epicurus_core_app.automations.runner import (
    AUTOMATION_FAILED,
    AutomationMatcher,
    AutomationRunner,
    AutomationScheduler,
    _schedule_due,
)
from epicurus_core_app.automations.sinks import SinkDispatcher
from epicurus_core_app.automations.store import (
    AutomationQueue,
    AutomationStore,
    KillSwitchStore,
)
from epicurus_core_app.event_log import LoggedEvent
from epicurus_core_app.scheduled_turns import ScheduledTurnStore

TENANT = "local"


# ── fakes ────────────────────────────────────────────────────────────────────


class _FakePower:
    def __init__(self, paused: bool = False) -> None:
        self.paused = paused


class _FakeAgent:
    """Records each turn it is asked to run; can be made to fail."""

    def __init__(self, answer: str = "done", fail: bool = False) -> None:
        self.answer = answer
        self.fail = fail
        self.calls: list[dict[str, object]] = []

    async def run(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        tenant_id: str | None = None,
        session_id: str | None = None,
        allow: frozenset[str] | None = None,
        automation_id: str | None = None,
    ) -> AgentTurn:
        self.calls.append(
            {
                "prompt": messages[0].content,
                "model": model,
                "tenant_id": tenant_id,
                "session_id": session_id,
                "allow": allow,
                "automation_id": automation_id,
            }
        )
        if self.fail:
            raise RuntimeError("boom")
        return AgentTurn(
            content=self.answer,
            stopped="completed",
            usage=TurnUsage(prompt_tokens=10, completion_tokens=5, steps=1),
        )


class _RecordingBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, object]]] = []

    async def publish(self, subject: str, data: object, tenant_id: str | None = None) -> None:
        assert isinstance(data, dict)
        self.published.append((subject, data))


async def _tz() -> str:
    return "UTC"


def _engine():
    return create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )


async def _fresh() -> tuple[AutomationStore, AutomationQueue, KillSwitchStore]:
    engine = _engine()
    store, queue, kill = AutomationStore(engine), AutomationQueue(engine), KillSwitchStore(engine)
    await store.init()
    await queue.init()
    await kill.init()
    return store, queue, kill


def _event(
    *,
    module: str = "echo",
    event_type: str = "echo.pinged",
    payload: dict[str, object] | None = None,
    causation_id: str | None = None,
    tenant: str = TENANT,
    event_id: int = 1,
) -> LoggedEvent:
    return LoggedEvent(
        id=event_id,
        tenant=tenant,
        module=module,
        type=event_type,
        occurred_at=datetime.now(UTC),
        received_at=datetime.now(UTC),
        dedup_key=f"k{event_id}",
        entity_ref=EntityRef(ref_id="e1", module=module, kind="ping", title="a ping"),
        payload=payload or {},
        schema_version=1,
        causation_id=causation_id,
    )


async def _an_automation(store: AutomationStore, **overrides: object) -> Automation:
    kwargs: dict[str, object] = {
        "tenant": TENANT,
        "name": "Tell me",
        "prompt": "Say something.",
        "autonomy": "notify",
        "event_trigger": EventTrigger(module="echo", event_type="echo.pinged"),
        "sinks": ["chat"],
    }
    kwargs.update(overrides)
    return await store.create(**kwargs)  # type: ignore[arg-type]


def _runner(
    store: AutomationStore,
    queue: AutomationQueue,
    kill: KillSwitchStore,
    *,
    agent: _FakeAgent | None = None,
    power: _FakePower | None = None,
    sinks: SinkDispatcher | None = None,
    bus: _RecordingBus | None = None,
) -> AutomationRunner:
    return AutomationRunner(
        store,
        queue,
        agent or _FakeAgent(),  # type: ignore[arg-type]
        power or _FakePower(),  # type: ignore[arg-type]
        kill,
        sinks or SinkDispatcher(),
        bus=bus,  # type: ignore[arg-type]
    )


# ── the store ────────────────────────────────────────────────────────────────


async def test_create_and_list_is_tenant_scoped() -> None:
    store, _q, _k = await _fresh()
    await _an_automation(store, tenant=TENANT, name="mine")
    await _an_automation(store, tenant="other", name="theirs")
    assert [a.name for a in await store.list(tenant=TENANT)] == ["mine"]


async def test_list_enabled_spans_tenants_and_excludes_disabled() -> None:
    store, _q, _k = await _fresh()
    await _an_automation(store, tenant=TENANT, name="on")
    await _an_automation(store, tenant="other", name="also on")
    off = await _an_automation(store, name="off")
    await store.set_enabled(tenant=TENANT, automation_id=off.id, enabled=False)
    assert {a.name for a in await store.list_enabled()} == {"on", "also on"}


async def test_the_ledger_records_a_run() -> None:
    store, queue, kill = await _fresh()
    automation = await _an_automation(store)
    await _runner(store, queue, kill).run_once(
        automation, trigger_refs=[7], summaries=["echo.pinged"], verdict="matched"
    )
    runs = await store.runs(tenant=TENANT)
    assert len(runs) == 1
    assert runs[0].outcome == "ok"
    assert runs[0].trigger_refs == [7]
    assert runs[0].filter_verdict == "matched"
    assert runs[0].output == "done"


async def test_the_ledger_carries_both_attributions_and_the_token_counts() -> None:
    # Dual metering: the row names the tenant *and* the automation, and records what the
    # turn actually cost. Without both, an automation quietly burning tokens is
    # indistinguishable from the operator's own chatting.
    store, queue, kill = await _fresh()
    automation = await _an_automation(store, model="qwen2.5:7b")
    await _runner(store, queue, kill).run_once(
        automation, trigger_refs=[], summaries=[], verdict="manual"
    )
    run = (await store.runs(tenant=TENANT))[0]
    assert run.tenant == TENANT
    assert run.automation_id == automation.id
    assert run.model == "qwen2.5:7b"
    assert run.prompt_tokens == 10
    assert run.completion_tokens == 5
    assert run.duration_ms is not None


async def test_the_gateway_call_is_attributed_to_the_automation() -> None:
    # The other half of the dual attribution: the usage event the SaaS overlay meters on.
    store, queue, kill = await _fresh()
    agent = _FakeAgent()
    automation = await _an_automation(store)
    await _runner(store, queue, kill, agent=agent).run_once(
        automation, trigger_refs=[], summaries=[], verdict="manual"
    )
    assert agent.calls[0]["automation_id"] == automation.id
    assert agent.calls[0]["tenant_id"] == TENANT


# ── the autonomy dial reaches the turn ───────────────────────────────────────


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        ("notify", {"read"}),
        ("propose", {"read", "propose"}),
        ("act", {"read", "propose", "write"}),
        ("silent_act", {"read", "propose", "write"}),
    ],
)
async def test_the_run_hands_the_turn_its_level_allowance(level: str, expected: set[str]) -> None:
    # The dial is applied where it matters: at the turn, not in the prompt.
    store, queue, kill = await _fresh()
    agent = _FakeAgent()
    automation = await _an_automation(store, autonomy=level)
    await _runner(store, queue, kill, agent=agent).run_once(
        automation, trigger_refs=[], summaries=[], verdict="manual"
    )
    assert agent.calls[0]["allow"] == frozenset(expected)


# ── safety: the kill switch ──────────────────────────────────────────────────


async def test_the_kill_switch_stops_a_run_entirely() -> None:
    store, queue, kill = await _fresh()
    agent = _FakeAgent()
    automation = await _an_automation(store)
    await kill.set_halted(tenant=TENANT, halted=True)
    run = await _runner(store, queue, kill, agent=agent).run_once(
        automation, trigger_refs=[], summaries=[], verdict="matched"
    )
    assert run is None
    assert agent.calls == []
    assert await store.runs(tenant=TENANT) == []  # nothing ran, so nothing is recorded


async def test_the_kill_switch_is_tenant_scoped() -> None:
    store, queue, kill = await _fresh()
    await kill.set_halted(tenant="other", halted=True)
    automation = await _an_automation(store, tenant=TENANT)
    run = await _runner(store, queue, kill).run_once(
        automation, trigger_refs=[], summaries=[], verdict="matched"
    )
    assert run is not None  # halting one tenant must not halt another


async def test_the_kill_switch_survives_a_new_store_instance() -> None:
    # The deliberate departure from PowerController: a stop a restart silently undoes is
    # not a stop. Two stores over one engine stand in for a process restart.
    engine = _engine()
    first = KillSwitchStore(engine)
    await first.init()
    await first.set_halted(tenant=TENANT, halted=True)
    second = KillSwitchStore(engine)
    assert await second.halted(tenant=TENANT) is True


async def test_resuming_lets_runs_through_again() -> None:
    store, queue, kill = await _fresh()
    automation = await _an_automation(store)
    await kill.set_halted(tenant=TENANT, halted=True)
    await kill.set_halted(tenant=TENANT, halted=False)
    assert (
        await _runner(store, queue, kill).run_once(
            automation, trigger_refs=[], summaries=[], verdict="matched"
        )
        is not None
    )


# ── safety: power pause ──────────────────────────────────────────────────────


async def test_a_paused_runtime_skips_and_records() -> None:
    # Skip *and* record: recording advances last_run_at so the window isn't re-evaluated
    # every tick, and the operator can see why nothing arrived.
    store, queue, kill = await _fresh()
    agent = _FakeAgent()
    automation = await _an_automation(store)
    run = await _runner(store, queue, kill, agent=agent, power=_FakePower(paused=True)).run_once(
        automation, trigger_refs=[], summaries=[], verdict="matched"
    )
    assert agent.calls == []
    assert run is not None
    assert run.outcome == "skipped"
    assert run.error == "runtime paused"


# ── safety: rate caps ────────────────────────────────────────────────────────


async def test_the_rate_cap_stops_a_run_and_records_why() -> None:
    store, queue, kill = await _fresh()
    agent = _FakeAgent()
    automation = await _an_automation(store, rate_cap_per_hour=2)
    runner = _runner(store, queue, kill, agent=agent)
    for _ in range(3):
        await runner.run_once(automation, trigger_refs=[], summaries=[], verdict="matched")
    assert len(agent.calls) == 2  # the third never reached the model
    runs = await store.runs(tenant=TENANT)
    assert [r.outcome for r in runs] == ["skipped", "ok", "ok"]  # newest first
    assert "rate cap" in (runs[0].error or "")


async def test_a_zero_rate_cap_is_uncapped() -> None:
    store, queue, kill = await _fresh()
    agent = _FakeAgent()
    automation = await _an_automation(store, rate_cap_per_hour=0)
    runner = _runner(store, queue, kill, agent=agent)
    for _ in range(5):
        await runner.run_once(automation, trigger_refs=[], summaries=[], verdict="matched")
    assert len(agent.calls) == 5


async def test_a_failing_run_consumes_rate_budget() -> None:
    # An automation failing in a loop is exactly what a cap is for, so a failure must
    # count. Counting only successes would make the cap useless in the one case it matters.
    store, queue, kill = await _fresh()
    agent = _FakeAgent(fail=True)
    automation = await _an_automation(store, rate_cap_per_hour=2)
    runner = _runner(store, queue, kill, agent=agent)
    for _ in range(3):
        await runner.run_once(automation, trigger_refs=[], summaries=[], verdict="matched")
    assert len(agent.calls) == 2


# ── failures ─────────────────────────────────────────────────────────────────


async def test_a_failing_run_is_recorded_not_raised() -> None:
    store, queue, kill = await _fresh()
    automation = await _an_automation(store)
    run = await _runner(store, queue, kill, agent=_FakeAgent(fail=True)).run_once(
        automation, trigger_refs=[], summaries=[], verdict="matched"
    )
    assert run is not None
    assert run.outcome == "error"
    assert "boom" in (run.error or "")


async def test_a_failure_announces_core_automation_failed() -> None:
    store, queue, kill = await _fresh()
    bus = _RecordingBus()
    automation = await _an_automation(store)
    await _runner(store, queue, kill, agent=_FakeAgent(fail=True), bus=bus).run_once(
        automation, trigger_refs=[], summaries=[], verdict="matched"
    )
    subject, data = bus.published[0]
    assert subject == f"events.{AUTOMATION_FAILED}"
    assert data["module"] == "core"
    assert data["payload"]["automation_id"] == automation.id  # type: ignore[index]
    # The failure event carries a causation id, so an automation's failure can never
    # itself trigger an automation.
    assert data["causation_id"] == automation.id


async def test_repeat_failures_are_rate_limited() -> None:
    # A broken automation on a chatty trigger would otherwise turn its own failures into
    # a firehose — into the very log the operator is trying to read.
    store, queue, kill = await _fresh()
    bus = _RecordingBus()
    automation = await _an_automation(store)
    runner = _runner(store, queue, kill, agent=_FakeAgent(fail=True), bus=bus)
    for _ in range(4):
        await runner.run_once(automation, trigger_refs=[], summaries=[], verdict="matched")
    assert len(bus.published) == 1  # one announcement, four failures


async def test_a_failure_without_a_bus_still_records() -> None:
    store, queue, kill = await _fresh()
    automation = await _an_automation(store)
    run = await _runner(store, queue, kill, agent=_FakeAgent(fail=True), bus=None).run_once(
        automation, trigger_refs=[], summaries=[], verdict="matched"
    )
    assert run is not None and run.outcome == "error"


# ── the loop guard ───────────────────────────────────────────────────────────


async def test_the_loop_guard_refuses_an_automation_produced_event() -> None:
    # The proof: an event carrying a causation id never queues a trigger, so an
    # automation cannot feed itself — or another automation, or a cycle of them.
    store, queue, _k = await _fresh()
    automation = await _an_automation(store)
    matcher = AutomationMatcher(store, queue, timezone=_tz)
    await matcher.on_event(_event(causation_id=automation.id))
    assert await queue.count() == 0


async def test_the_loop_guard_refuses_events_caused_by_any_automation() -> None:
    # Depth-1 and blunt on purpose: A→B→A is a loop too. An event caused by a *different*
    # automation is refused just the same.
    store, queue, _k = await _fresh()
    await _an_automation(store)
    matcher = AutomationMatcher(store, queue, timezone=_tz)
    await matcher.on_event(_event(causation_id="some-other-automation"))
    assert await queue.count() == 0


async def test_an_ordinary_module_event_still_queues() -> None:
    store, queue, _k = await _fresh()
    automation = await _an_automation(store)
    matcher = AutomationMatcher(store, queue, timezone=_tz)
    await matcher.on_event(_event())
    pending = await queue.pending()
    assert [p.automation_id for p in pending] == [automation.id]


# ── the matcher ──────────────────────────────────────────────────────────────


async def test_the_matcher_ignores_a_non_matching_event() -> None:
    store, queue, _k = await _fresh()
    await _an_automation(
        store, event_trigger=EventTrigger(module="mail", event_type="mail.received")
    )
    matcher = AutomationMatcher(store, queue, timezone=_tz)
    await matcher.on_event(_event(module="echo", event_type="echo.pinged"))
    assert await queue.count() == 0


async def test_the_matcher_is_tenant_scoped() -> None:
    store, queue, _k = await _fresh()
    await _an_automation(store, tenant="other")
    matcher = AutomationMatcher(store, queue, timezone=_tz)
    await matcher.on_event(_event(tenant=TENANT))
    assert await queue.count() == 0


async def test_the_matcher_applies_payload_matchers() -> None:
    store, queue, _k = await _fresh()
    await _an_automation(
        store,
        event_trigger=EventTrigger(
            module="echo",
            event_type="echo.pinged",
            matchers=[PayloadMatcher(field="note", op="contains", value="urgent")],
        ),
    )
    matcher = AutomationMatcher(store, queue, timezone=_tz)
    await matcher.on_event(_event(payload={"note": "just saying hi"}, event_id=1))
    assert await queue.count() == 0
    await matcher.on_event(_event(payload={"note": "urgent thing"}, event_id=2))
    assert await queue.count() == 1


async def test_a_disabled_automation_never_matches() -> None:
    store, queue, _k = await _fresh()
    automation = await _an_automation(store)
    await store.set_enabled(tenant=TENANT, automation_id=automation.id, enabled=False)
    matcher = AutomationMatcher(store, queue, timezone=_tz)
    await matcher.on_event(_event())
    assert await queue.count() == 0


# ── the digest window ────────────────────────────────────────────────────────


async def test_no_digest_window_runs_per_event() -> None:
    store, queue, kill = await _fresh()
    agent = _FakeAgent()
    automation = await _an_automation(store, digest_window_minutes=0)
    for i in (1, 2):
        await queue.enqueue(tenant=TENANT, automation_id=automation.id, event_id=i, summary=f"e{i}")
    scheduler = AutomationScheduler(
        store, queue, _runner(store, queue, kill, agent=agent), timezone=_tz
    )
    await scheduler.drain_queue()
    # One run carrying both pending triggers — the drain batches what is waiting; the
    # window only decides *when*, not whether, to batch.
    assert len(agent.calls) == 1
    assert await queue.count() == 0


async def test_an_open_digest_window_holds_the_run() -> None:
    store, queue, kill = await _fresh()
    agent = _FakeAgent()
    automation = await _an_automation(store, digest_window_minutes=30)
    await queue.enqueue(tenant=TENANT, automation_id=automation.id, event_id=1, summary="e1")
    scheduler = AutomationScheduler(
        store, queue, _runner(store, queue, kill, agent=agent), timezone=_tz
    )
    await scheduler.drain_queue()
    assert agent.calls == []  # still collecting
    assert await queue.count() == 1  # and nothing was lost


async def test_a_closed_digest_window_batches_into_one_run() -> None:
    store, queue, kill = await _fresh()
    agent = _FakeAgent()
    automation = await _an_automation(store, digest_window_minutes=30)
    for i in (1, 2, 3):
        await queue.enqueue(
            tenant=TENANT, automation_id=automation.id, event_id=i, summary=f"event-{i}"
        )
    # Age the queue past the window rather than sleeping 30 minutes.
    await _age_queue(queue, minutes=31)
    scheduler = AutomationScheduler(
        store, queue, _runner(store, queue, kill, agent=agent), timezone=_tz
    )
    await scheduler.drain_queue()
    assert len(agent.calls) == 1  # three events, one run — the point of a digest
    prompt = str(agent.calls[0]["prompt"])
    assert "event-1" in prompt and "event-3" in prompt
    run = (await store.runs(tenant=TENANT))[0]
    assert run.filter_verdict == "digest"
    assert run.trigger_refs == [1, 2, 3]
    assert await queue.count() == 0


async def _age_queue(queue: AutomationQueue, *, minutes: int) -> None:
    """Backdate every queued trigger, so a window test needn't wait for real time."""
    from epicurus_core_app.automations.store import _StoredQueueItem

    async with queue._session() as session:
        rows = await session.scalars(__import__("sqlalchemy").select(_StoredQueueItem))
        for row in rows:
            row.created_at = datetime.now(UTC) - timedelta(minutes=minutes)
        await session.commit()


async def test_a_deleted_automations_queued_work_is_dropped() -> None:
    # Otherwise a paused/removed automation's backlog would replay in full whenever it
    # came back — a burst of stale events, hours later.
    store, queue, kill = await _fresh()
    automation = await _an_automation(store)
    await queue.enqueue(tenant=TENANT, automation_id=automation.id, event_id=1, summary="e")
    await store.delete(tenant=TENANT, automation_id=automation.id)
    scheduler = AutomationScheduler(store, queue, _runner(store, queue, kill), timezone=_tz)
    await scheduler.drain_queue()
    assert await queue.count() == 0


async def test_a_halted_tenants_triggers_stay_queued() -> None:
    # Resuming should deliver what was held, not silently discard it.
    store, queue, kill = await _fresh()
    automation = await _an_automation(store)
    await kill.set_halted(tenant=TENANT, halted=True)
    await queue.enqueue(tenant=TENANT, automation_id=automation.id, event_id=1, summary="e")
    scheduler = AutomationScheduler(store, queue, _runner(store, queue, kill), timezone=_tz)
    await scheduler.drain_queue()
    assert await queue.count() == 1


# ── schedule triggers ────────────────────────────────────────────────────────


def _scheduled(hour: int, **overrides: object) -> Automation:
    base: dict[str, object] = {
        "id": "a1",
        "tenant": TENANT,
        "name": "n",
        "enabled": True,
        "source": "user",
        "event_trigger": None,
        "schedule_trigger": ScheduleTrigger(cadence="daily", hour=hour),
        "prompt": "p",
        "model": None,
        "autonomy": "notify",
        "sinks": ["chat"],
        "chat_mode": "rolling",
        "chat_session_id": None,
        "rate_cap_per_hour": 0,
        "digest_window_minutes": 0,
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return Automation(**base)  # type: ignore[arg-type]


def test_a_daily_schedule_is_due_at_its_hour_when_never_run() -> None:
    now = datetime(2026, 7, 17, 7, 30, tzinfo=UTC)
    assert _schedule_due(_scheduled(7), now, 7) is True


def test_not_due_at_another_hour() -> None:
    now = datetime(2026, 7, 17, 9, 0, tzinfo=UTC)
    assert _schedule_due(_scheduled(7), now, 9) is False


def test_not_due_twice_in_the_same_day() -> None:
    now = datetime(2026, 7, 17, 7, 30, tzinfo=UTC)
    already = _scheduled(7, last_run_at=datetime(2026, 7, 17, 7, 5, tzinfo=UTC))
    assert _schedule_due(already, now, 7) is False


def test_due_again_the_next_day() -> None:
    now = datetime(2026, 7, 18, 7, 30, tzinfo=UTC)
    ran_yesterday = _scheduled(7, last_run_at=datetime(2026, 7, 17, 7, 5, tzinfo=UTC))
    assert _schedule_due(ran_yesterday, now, 7) is True


def test_a_weekly_schedule_waits_for_its_weekday() -> None:
    friday = datetime(2026, 7, 17, 7, 30, tzinfo=UTC)  # a Friday (weekday 4)
    weekly = _scheduled(7, schedule_trigger=ScheduleTrigger(cadence="weekly", hour=7, weekday=0))
    assert _schedule_due(weekly, friday, 7) is False
    on_monday = _scheduled(7, schedule_trigger=ScheduleTrigger(cadence="weekly", hour=7, weekday=4))
    assert _schedule_due(on_monday, friday, 7) is True


async def test_the_tick_fires_a_due_schedule() -> None:
    store, queue, kill = await _fresh()
    agent = _FakeAgent()
    hour = datetime.now(UTC).hour
    await _an_automation(
        store,
        event_trigger=None,
        schedule_trigger=ScheduleTrigger(cadence="daily", hour=hour),
    )
    scheduler = AutomationScheduler(
        store, queue, _runner(store, queue, kill, agent=agent), timezone=_tz
    )
    await scheduler.tick_schedules()
    assert len(agent.calls) == 1
    assert (await store.runs(tenant=TENANT))[0].filter_verdict == "schedule"


async def test_the_loop_survives_a_failing_tick() -> None:
    store, queue, kill = await _fresh()
    scheduler = AutomationScheduler(
        store, queue, _runner(store, queue, kill), timezone=_tz, poll_interval_s=0
    )
    calls = 0

    async def _boom() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("db down")

    scheduler.drain_queue = _boom  # type: ignore[method-assign]
    task = asyncio.create_task(scheduler.run_periodic())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls > 1  # it kept ticking after the failure


# ── sinks ────────────────────────────────────────────────────────────────────


async def test_sinks_receive_the_output() -> None:
    store, queue, kill = await _fresh()
    seen: list[str] = []

    async def _notes(_a: Automation, _o: str) -> None:
        seen.append(_o)

    # A post-run fan-out sink (notes); chat is turn-time and no longer dispatched (#672).
    sinks = SinkDispatcher()
    sinks.register("notes", _notes)
    automation = await _an_automation(store, sinks=["notes"])
    run = await _runner(store, queue, kill, sinks=sinks).run_once(
        automation, trigger_refs=[], summaries=[], verdict="matched"
    )
    assert seen == ["done"]
    assert run is not None and run.sinks_fired == ["notes"]


async def test_silent_act_fires_no_sink_but_still_records() -> None:
    # The whole point of the level: it acts and reports to the ledger alone.
    store, queue, kill = await _fresh()
    seen: list[str] = []

    async def _chat(_a: Automation, output: str) -> None:
        seen.append(output)

    sinks = SinkDispatcher()
    sinks.register("chat", _chat)
    automation = await _an_automation(store, autonomy="silent_act", sinks=["chat"])
    run = await _runner(store, queue, kill, sinks=sinks).run_once(
        automation, trigger_refs=[], summaries=[], verdict="matched"
    )
    assert seen == []
    assert run is not None
    assert run.sinks_fired == []
    assert run.output == "done"  # recorded — the only trace there is


async def test_an_unregistered_sink_degrades_gracefully() -> None:
    # Its companion issue hasn't landed. The run is still complete and the output is
    # still on the ledger — unannounced, never lost.
    store, queue, kill = await _fresh()
    automation = await _an_automation(store, sinks=["push"])
    run = await _runner(store, queue, kill).run_once(
        automation, trigger_refs=[], summaries=[], verdict="matched"
    )
    assert run is not None
    assert run.outcome == "ok"
    assert run.sinks_fired == []
    assert run.output == "done"


async def test_a_failing_sink_does_not_cost_the_others() -> None:
    store, queue, kill = await _fresh()
    delivered: list[str] = []

    async def _broken(_a: Automation, _o: str) -> None:
        raise RuntimeError("push service down")

    async def _works(_a: Automation, output: str) -> None:
        delivered.append(output)

    sinks = SinkDispatcher()
    sinks.register("push", _broken)
    sinks.register("notes", _works)
    automation = await _an_automation(store, sinks=["push", "notes"])
    run = await _runner(store, queue, kill, sinks=sinks).run_once(
        automation, trigger_refs=[], summaries=[], verdict="matched"
    )
    assert delivered == ["done"]
    assert run is not None and run.sinks_fired == ["notes"]


async def test_the_dispatcher_reports_what_it_could_not_do() -> None:
    sinks = SinkDispatcher()

    async def _broken(_a: Automation, _o: str) -> None:
        raise RuntimeError("down")

    sinks.register("push", _broken)
    automation = _scheduled(7, sinks=["push", "kb"])
    result = await sinks.dispatch(automation, "out")
    assert result.failed == ["push"]
    assert result.unavailable == ["kb"]  # kb has no handler here; chat is skipped, not reported
    assert result.fired == []


# ── the prompt ───────────────────────────────────────────────────────────────


async def test_the_triggering_events_reach_the_prompt_as_context() -> None:
    store, queue, kill = await _fresh()
    agent = _FakeAgent()
    automation = await _an_automation(store, prompt="Summarize.")
    await _runner(store, queue, kill, agent=agent).run_once(
        automation, trigger_refs=[1], summaries=["echo.pinged (a ping)"], verdict="matched"
    )
    prompt = str(agent.calls[0]["prompt"])
    assert "Summarize." in prompt
    assert "echo.pinged (a ping)" in prompt
    # Framed as data, not instructions — this is the boundary where a mail subject line
    # would otherwise get to dictate the assistant's behaviour.
    assert "not instructions to follow" in prompt


async def test_a_schedule_run_prompt_is_just_the_instructions() -> None:
    store, queue, kill = await _fresh()
    agent = _FakeAgent()
    automation = await _an_automation(store, prompt="Morning briefing.")
    await _runner(store, queue, kill, agent=agent).run_once(
        automation, trigger_refs=[], summaries=[], verdict="schedule"
    )
    assert agent.calls[0]["prompt"] == "Morning briefing."


async def test_rolling_chat_mode_reuses_one_session() -> None:
    store, queue, kill = await _fresh()
    agent = _FakeAgent()
    automation = await _an_automation(store, chat_mode="rolling")
    runner = _runner(store, queue, kill, agent=agent)
    await runner.run_once(automation, trigger_refs=[], summaries=[], verdict="manual")
    await runner.run_once(automation, trigger_refs=[], summaries=[], verdict="manual")
    assert agent.calls[0]["session_id"] == agent.calls[1]["session_id"]


async def test_per_run_chat_mode_opens_a_fresh_session() -> None:
    store, queue, kill = await _fresh()
    agent = _FakeAgent()
    automation = await _an_automation(store, chat_mode="per_run")
    runner = _runner(store, queue, kill, agent=agent)
    await runner.run_once(automation, trigger_refs=[], summaries=[], verdict="manual")
    await runner.run_once(automation, trigger_refs=[], summaries=[], verdict="manual")
    assert agent.calls[0]["session_id"] != agent.calls[1]["session_id"]


# ── the scheduled-turns fold-in ──────────────────────────────────────────────


async def _turn_store() -> ScheduledTurnStore:
    store = ScheduledTurnStore(_engine())
    await store.init()
    return store


async def test_a_scheduled_turn_migrates_into_an_automation() -> None:
    turns = await _turn_store()
    store, _q, _k = await _fresh()
    await turns.create(
        tenant=TENANT,
        prompt="Summarize today's calendar",
        cadence="daily",
        hour=7,
        weekday=None,
        delivery_target="scheduled-abc",
    )
    assert await migrate_scheduled_turns(turns, store) == 1

    automation = (await store.list(tenant=TENANT))[0]
    assert automation.prompt == "Summarize today's calendar"
    assert automation.schedule_trigger == ScheduleTrigger(cadence="daily", hour=7, weekday=None)
    assert automation.sinks == ["chat"]
    assert automation.chat_mode == "rolling"
    # Its existing session, so the operator's history stays where they left it.
    assert automation.chat_session_id == "scheduled-abc"
    # What it already was: the headless path could only ever summarize.
    assert automation.autonomy == "notify"


async def test_the_migration_is_idempotent() -> None:
    turns = await _turn_store()
    store, _q, _k = await _fresh()
    await turns.create(
        tenant=TENANT, prompt="p", cadence="daily", hour=7, weekday=None, delivery_target="s"
    )
    assert await migrate_scheduled_turns(turns, store) == 1
    assert await migrate_scheduled_turns(turns, store) == 0  # a second boot is a no-op
    assert len(await store.list(tenant=TENANT)) == 1


async def test_the_migration_is_non_destructive() -> None:
    # The source row survives, marked rather than deleted: a migration that drops data on
    # first boot has no way back if it turns out to be wrong.
    turns = await _turn_store()
    store, _q, _k = await _fresh()
    await turns.create(
        tenant=TENANT, prompt="p", cadence="daily", hour=7, weekday=None, delivery_target="s"
    )
    await migrate_scheduled_turns(turns, store)
    surviving = await turns.list(tenant=TENANT)
    assert len(surviving) == 1
    assert surviving[0].last_status == MIGRATED_MARKER


async def test_a_disabled_turn_migrates_disabled() -> None:
    # Dropping it would silently delete a paused thing the operator deliberately kept.
    turns = await _turn_store()
    store, _q, _k = await _fresh()
    turn = await turns.create(
        tenant=TENANT, prompt="p", cadence="daily", hour=7, weekday=None, delivery_target="s"
    )
    await turns.set_enabled(tenant=TENANT, turn_id=turn.id, enabled=False)
    await migrate_scheduled_turns(turns, store)
    assert (await store.list(tenant=TENANT))[0].enabled is False


async def test_a_weekly_turn_keeps_its_weekday() -> None:
    turns = await _turn_store()
    store, _q, _k = await _fresh()
    await turns.create(
        tenant=TENANT, prompt="p", cadence="weekly", hour=9, weekday=2, delivery_target="s"
    )
    await migrate_scheduled_turns(turns, store)
    trigger = (await store.list(tenant=TENANT))[0].schedule_trigger
    assert trigger == ScheduleTrigger(cadence="weekly", hour=9, weekday=2)


async def test_a_turn_that_already_ran_today_does_not_run_again_on_migration() -> None:
    # The due-ness check reads last_run_at, so dropping it would make every migrated turn
    # fire once more the moment it became an automation.
    turns = await _turn_store()
    store, _q, _k = await _fresh()
    turn = await turns.create(
        tenant=TENANT, prompt="p", cadence="daily", hour=7, weekday=None, delivery_target="s"
    )
    ran_at = datetime.now(UTC)
    await turns.mark_run(turn_id=turn.id, status="ok", ran_at=ran_at)
    await migrate_scheduled_turns(turns, store)
    migrated = (await store.list(tenant=TENANT))[0]
    assert migrated.last_run_at is not None


async def test_the_migration_spans_tenants() -> None:
    turns = await _turn_store()
    store, _q, _k = await _fresh()
    for tenant in (TENANT, "other"):
        await turns.create(
            tenant=tenant, prompt="p", cadence="daily", hour=7, weekday=None, delivery_target="s"
        )
    assert await migrate_scheduled_turns(turns, store) == 2
    assert len(await store.list(tenant=TENANT)) == 1
    assert len(await store.list(tenant="other")) == 1
