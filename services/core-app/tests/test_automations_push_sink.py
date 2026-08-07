"""The push sink (#723) — the last of the four ADR-0105 sinks to get a handler.

Two layers: `make_push_sink` in isolation against a fake `Notifier` (title/body mapping,
category, deep link, the EntityRef artifact decision), then end to end through a real
`AutomationRunner` + `PushService` + `NotificationStore` + `PushPrefsStore` stack — proving
the handler goes *through* `notify()` rather than around it, so quiet hours / the rate cap /
the category and per-automation toggles all apply exactly as they do for any other caller.

File-backed SQLite per test (AGENTS.md's StaticPool pitfall): the runner and PushService both
touch the store, matching the convention `test_automations_sinks.py` already established for
this package. No device subscriptions are ever registered — every test here is about the
gating/recording contract, not webpush wire mechanics (that's `test_push_service.py`'s job),
so `_send_now` always short-circuits to `skipped_no_devices` with no network call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from epicurus_core import AutomationTemplate, EntityRef, SecretError
from epicurus_core_app.agent.agent import AgentTurn, TurnUsage
from epicurus_core_app.automations.model import Automation, EventTrigger, ScheduleTrigger
from epicurus_core_app.automations.push_sink import PUSH_CATEGORY, make_push_sink
from epicurus_core_app.automations.runner import AutomationRunner
from epicurus_core_app.automations.sinks import SinkDispatcher
from epicurus_core_app.automations.store import AutomationQueue, AutomationStore, KillSwitchStore
from epicurus_core_app.notifications import NotificationStore
from epicurus_core_app.push.prefs import ChannelPrefs, PushPrefsStore
from epicurus_core_app.push.queue import PushQueueStore
from epicurus_core_app.push.service import NotifyResult, PushService
from epicurus_core_app.push.subscriptions import PushSubscriptionStore

TENANT = "local"


# ── make_push_sink in isolation ─────────────────────────────────────────────────


class _FakeNotifier:
    """Records every ``notify()`` call; returns a canned result — no push stack needed."""

    def __init__(self, *, notification_id: str | None = "note-1") -> None:
        self._notification_id = notification_id
        self.calls: list[dict[str, Any]] = []

    async def notify(
        self,
        tenant: str,
        *,
        category: str,
        title: str,
        body: str,
        deep_link: str | None = None,
        entity_ref: dict[str, Any] | None = None,
        automation_id: str | None = None,
    ) -> NotifyResult:
        self.calls.append(
            {
                "tenant": tenant,
                "category": category,
                "title": title,
                "body": body,
                "deep_link": deep_link,
                "entity_ref": entity_ref,
                "automation_id": automation_id,
            }
        )
        return NotifyResult(outcome="sent", sent_count=1, notification_id=self._notification_id)


def _automation(**overrides: Any) -> Automation:
    defaults: dict[str, Any] = {
        "id": "a1",
        "tenant": TENANT,
        "name": "Triage new mail",
        "enabled": True,
        "source": "user",
        "event_trigger": None,
        "schedule_trigger": ScheduleTrigger(cadence="daily", hour=9),
        "prompt": "do",
        "model": None,
        "autonomy": "notify",
        "sinks": ["push"],
        "chat_mode": "rolling",
        "chat_session_id": None,
        "rate_cap_per_hour": 0,
        "digest_window_minutes": 0,
        "created_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return Automation(**defaults)


async def test_push_sink_notifies_under_the_automation_category() -> None:
    notifier = _FakeNotifier()
    handler = make_push_sink(notifier)
    await handler(_automation(), "the run's answer")
    call = notifier.calls[0]
    assert call["category"] == PUSH_CATEGORY == "automation"
    assert call["tenant"] == TENANT
    assert call["automation_id"] == "a1"


async def test_push_sink_title_is_the_automations_own_name() -> None:
    notifier = _FakeNotifier()
    handler = make_push_sink(notifier)
    await handler(_automation(name="Morning unread digest"), "output")
    assert notifier.calls[0]["title"] == "Morning unread digest"


async def test_push_sink_body_is_the_runs_output_untruncated() -> None:
    """The sink hands notify() the full text; PushService's own wire layer is what shortens
    the outgoing push payload — the center row must keep every character."""
    notifier = _FakeNotifier()
    handler = make_push_sink(notifier)
    long_output = "x" * 5000
    await handler(_automation(), long_output)
    assert notifier.calls[0]["body"] == long_output


async def test_push_sink_falls_back_to_a_placeholder_body_when_output_is_blank() -> None:
    notifier = _FakeNotifier()
    handler = make_push_sink(notifier)
    await handler(_automation(), "   ")
    assert notifier.calls[0]["body"] == "The run completed with no output."


async def test_push_sink_defensively_truncates_a_very_long_automation_name() -> None:
    notifier = _FakeNotifier()
    handler = make_push_sink(notifier)
    await handler(_automation(name="x" * 300), "out")
    assert len(notifier.calls[0]["title"]) == 200


async def test_push_sink_deep_links_to_this_automations_run_history() -> None:
    notifier = _FakeNotifier()
    handler = make_push_sink(notifier)
    await handler(_automation(id="auto-42"), "out")
    assert notifier.calls[0]["deep_link"] == "/observability?tab=runs&automation=auto-42"


async def test_push_sink_returns_an_entity_ref_when_a_center_row_was_written() -> None:
    notifier = _FakeNotifier(notification_id="note-99")
    handler = make_push_sink(notifier)
    ref = await handler(_automation(name="Weekly review"), "out")
    assert ref == EntityRef(
        ref_id="note-99", module="core", kind="notification", title="Weekly review"
    )


async def test_push_sink_returns_no_artifact_when_the_center_toggle_is_off() -> None:
    """No durable row exists to point at — the one case notes/kb never hits."""
    notifier = _FakeNotifier(notification_id=None)
    handler = make_push_sink(notifier)
    ref = await handler(_automation(), "out")
    assert ref is None


# ── end to end through the runner ────────────────────────────────────────────────


class _FakePower:
    def __init__(self, paused: bool = False) -> None:
        self.paused = paused


class _FakeAgent:
    def __init__(self, answer: str = "The run's report") -> None:
        self.answer = answer

    async def run(
        self,
        messages: list[Any],
        *,
        model: str | None = None,
        tenant_id: str | None = None,
        session_id: str | None = None,
        allow: frozenset[str] | None = None,
        automation_id: str | None = None,
        quiet_capable: bool = False,
    ) -> AgentTurn:
        return AgentTurn(
            content=self.answer,
            stopped="completed",
            usage=TurnUsage(prompt_tokens=10, completion_tokens=5, steps=1),
        )


class _FakeSecretStore:
    """Mirrors SecretStore's get/set contract without touching OpenBao."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], dict[str, Any]] = {}

    async def get(self, path: str, tenant_id: str | None = None) -> dict[str, Any]:
        key = (path, tenant_id or "")
        if key not in self._data:
            raise SecretError(f"not found: {path}")
        return self._data[key]

    async def set(self, path: str, data: dict[str, Any], tenant_id: str | None = None) -> None:
        self._data[(path, tenant_id or "")] = data


class _FakeEventBus:
    async def publish(self, subject: str, data: Any, tenant_id: str | None = None) -> None:
        return None


async def _utc() -> str:
    return "UTC"


def _schedule() -> ScheduleTrigger:
    return ScheduleTrigger(cadence="daily", hour=9)


@dataclass
class _Env:
    engine: AsyncEngine
    store: AutomationStore
    push_prefs: PushPrefsStore
    push_queue: PushQueueStore
    notifications: NotificationStore
    runner: AutomationRunner


async def _env(tmp_path: Any, *, rate_cap_per_hour: int = 0, timezone: Any = _utc) -> _Env:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'push_sink.db'}")
    store = AutomationStore(engine)
    queue = AutomationQueue(engine)
    kill = KillSwitchStore(engine)
    push_subscriptions = PushSubscriptionStore(engine)
    push_prefs = PushPrefsStore(engine)
    push_queue = PushQueueStore(engine)
    notifications = NotificationStore(engine)
    for s in (store, queue, kill, push_subscriptions, push_prefs, push_queue, notifications):
        await s.init()
    push_service = PushService(
        subscriptions=push_subscriptions,
        prefs=push_prefs,
        queue=push_queue,
        notifications=notifications,
        secrets=_FakeSecretStore(),  # type: ignore[arg-type]
        bus=_FakeEventBus(),  # type: ignore[arg-type]
        timezone=timezone,
        default_tenant=TENANT,
        vapid_subject="mailto:test@example.com",
        rate_cap_per_hour=rate_cap_per_hour,
    )
    sinks = SinkDispatcher()
    sinks.register("push", make_push_sink(push_service))
    runner = AutomationRunner(
        store,
        queue,
        _FakeAgent(),  # type: ignore[arg-type]
        _FakePower(),  # type: ignore[arg-type]
        kill,
        sinks,
    )
    return _Env(engine, store, push_prefs, push_queue, notifications, runner)


async def test_push_sink_records_a_notification_and_an_artifact_on_the_run(tmp_path: Any) -> None:
    env = await _env(tmp_path)
    automation = await env.store.create(
        tenant=TENANT,
        name="Triage new mail",
        prompt="do",
        autonomy="notify",
        schedule_trigger=_schedule(),
        sinks=["push"],
    )
    run = await env.runner.run_once(automation, trigger_refs=[], summaries=[], verdict="schedule")
    assert run is not None
    assert run.outcome == "ok"
    assert "push" in run.sinks_fired
    assert len(run.artifacts) == 1
    assert run.artifacts[0].module == "core"
    assert run.artifacts[0].kind == "notification"
    assert run.artifacts[0].title == "Triage new mail"
    rows = await env.notifications.list(TENANT)
    assert len(rows) == 1
    assert rows[0].category == "automation"
    assert rows[0].title == "Triage new mail"
    assert rows[0].body == "The run's report"
    assert rows[0].automation_id == automation.id
    assert rows[0].deep_link == f"/observability?tab=runs&automation={automation.id}"
    assert rows[0].id == run.artifacts[0].ref_id  # the artifact points at the row that was made
    await env.engine.dispose()


async def test_push_sink_still_records_the_center_row_during_quiet_hours(tmp_path: Any) -> None:
    """Acceptance property this sink inherits from ADR-0104 §1: a quiet-hours-suppressed push
    still lands in the center immediately, not deferred alongside the eventual digest."""
    env = await _env(tmp_path)
    await env.push_prefs.set_quiet_hours(TENANT, enabled=True, start="00:00", end="23:59")
    automation = await env.store.create(
        tenant=TENANT,
        name="Digest",
        prompt="do",
        autonomy="notify",
        schedule_trigger=_schedule(),
        sinks=["push"],
    )
    run = await env.runner.run_once(automation, trigger_refs=[], summaries=[], verdict="schedule")
    assert run is not None
    assert "push" in run.sinks_fired  # the handler completed without raising
    assert len(run.artifacts) == 1
    assert len(await env.notifications.list(TENANT)) == 1
    queued = await env.push_queue.list_for_tenant(TENANT)
    assert len(queued) == 1  # push itself was queued for the digest, not dropped
    await env.engine.dispose()


async def test_push_sink_records_no_artifact_when_the_category_toggle_is_off(tmp_path: Any) -> None:
    env = await _env(tmp_path)
    await env.push_prefs.set_categories(
        TENANT, {"automation": ChannelPrefs(push=False, center=False)}
    )
    automation = await env.store.create(
        tenant=TENANT,
        name="Digest",
        prompt="do",
        autonomy="notify",
        schedule_trigger=_schedule(),
        sinks=["push"],
    )
    run = await env.runner.run_once(automation, trigger_refs=[], summaries=[], verdict="schedule")
    assert run is not None
    assert "push" in run.sinks_fired  # dispatch still ran the handler successfully
    assert run.artifacts == []  # nothing durable was written to point at
    assert await env.notifications.list(TENANT) == []
    await env.engine.dispose()


async def test_push_sink_respects_a_per_automation_override(tmp_path: Any) -> None:
    """The seam ADR-0102 §4 built for this: silencing one automation without touching the
    "automation" category default for every other one."""
    env = await _env(tmp_path)
    automation = await env.store.create(
        tenant=TENANT,
        name="Noisy one",
        prompt="do",
        autonomy="notify",
        schedule_trigger=_schedule(),
        sinks=["push"],
    )
    await env.push_prefs.set_automation_override(
        TENANT, automation.id, ChannelPrefs(push=False, center=False)
    )
    run = await env.runner.run_once(automation, trigger_refs=[], summaries=[], verdict="schedule")
    assert run is not None
    assert run.artifacts == []
    assert await env.notifications.list(TENANT) == []
    await env.engine.dispose()


async def test_push_sink_respects_the_tenant_wide_rate_cap(tmp_path: Any) -> None:
    """The push rate cap only ever gates delivery, never the center write (ADR-0104 §1) —
    both runs still get a durable row even though the second push send is capped."""
    env = await _env(tmp_path, rate_cap_per_hour=1)
    automation = await env.store.create(
        tenant=TENANT,
        name="Frequent",
        prompt="do",
        autonomy="notify",
        schedule_trigger=_schedule(),
        sinks=["push"],
    )
    run_once = env.runner.run_once
    first = await run_once(automation, trigger_refs=[], summaries=[], verdict="schedule")
    second = await run_once(automation, trigger_refs=[], summaries=[], verdict="schedule")
    assert first is not None and second is not None
    assert "push" in first.sinks_fired
    assert "push" in second.sinks_fired
    assert len(await env.notifications.list(TENANT)) == 2
    await env.engine.dispose()


async def test_push_sink_is_tenant_scoped(tmp_path: Any) -> None:
    """Tenant A's run must never notify tenant B — constraint #1, exercised end to end."""
    env = await _env(tmp_path)
    automation_a = await env.store.create(
        tenant="tenant-a",
        name="A's automation",
        prompt="do",
        autonomy="notify",
        schedule_trigger=_schedule(),
        sinks=["push"],
    )
    automation_b = await env.store.create(
        tenant="tenant-b",
        name="B's automation",
        prompt="do",
        autonomy="notify",
        schedule_trigger=_schedule(),
        sinks=["push"],
    )
    await env.runner.run_once(automation_a, trigger_refs=[], summaries=[], verdict="schedule")
    await env.runner.run_once(automation_b, trigger_refs=[], summaries=[], verdict="schedule")
    a_rows = await env.notifications.list("tenant-a")
    b_rows = await env.notifications.list("tenant-b")
    assert [r.title for r in a_rows] == ["A's automation"]
    assert [r.title for r in b_rows] == ["B's automation"]
    assert all(r.tenant == "tenant-a" for r in a_rows)
    assert all(r.tenant == "tenant-b" for r in b_rows)
    await env.engine.dispose()


async def test_a_real_starter_template_instantiates_and_delivers_push(tmp_path: Any) -> None:
    """Mirrors what the web UI does when the operator hits "Use" on a template card: build an
    automation from a module's `AutomationTemplate` fields, `source="template:<module>"`. #717
    shipped ten of these with `sinks=["push"]`, and — until #723 — none of them delivered
    anything visible. This is mail's real "on-mail-received" template, reproduced field for
    field (`services/mail/src/epicurus_mail/service.py`), exercised at the store + runner
    level end to end."""
    template = AutomationTemplate(
        key="on-mail-received",
        name="Triage new mail",
        description=(
            "Runs a read-only turn whenever a new message arrives, so you get a quick sense"
            " of what it's about without opening your inbox."
        ),
        trigger={"module": "mail", "event_type": "mail.received"},
        prompt=(
            "A new email just arrived. Search for it and read it if you need to, then"
            " summarize it in one or two sentences, noting whether it looks important or"
            " time-sensitive."
        ),
        autonomy="notify",
        sinks=["push"],
    )
    env = await _env(tmp_path)
    automation = await env.store.create(
        tenant=TENANT,
        name=template.name,
        prompt=template.prompt,
        autonomy="notify",
        source=f"template:{template.trigger['module']}",
        event_trigger=EventTrigger(
            module=str(template.trigger["module"]), event_type=str(template.trigger["event_type"])
        ),
        sinks=list(template.sinks),  # type: ignore[arg-type]
    )
    assert automation.source == "template:mail"

    run = await env.runner.run_once(
        automation, trigger_refs=[1], summaries=["mail.received (Q3 invoice)"], verdict="matched"
    )

    assert run is not None
    assert run.outcome == "ok"
    assert "push" in run.sinks_fired
    rows = await env.notifications.list(TENANT)
    assert len(rows) == 1
    assert rows[0].title == "Triage new mail"
    assert rows[0].category == "automation"
    assert run.artifacts[0].ref_id == rows[0].id
    await env.engine.dispose()
