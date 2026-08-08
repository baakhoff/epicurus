"""Unit tests for EventAlertListener — match/dispatch/rate-cap for per-event alerts (#732),
plus the distinct decline-log lines its two gates emit (#797).

``Notifier`` is faked (a recording double), not the real ``PushService`` — this file tests
only the listener's own decision logic (subscribe/skip, rate cap, rendering); PushService's
own send-path behavior (quiet hours, its tenant-wide cap, webpush delivery) is covered by
test_push_service.py, including notify_effective's contribution to that path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core import EntityRef
from epicurus_core_app.event_log import LoggedEvent
from epicurus_core_app.push.event_alerts import EventAlertListener
from epicurus_core_app.push.event_subscriptions import EventSubscriptionStore
from epicurus_core_app.push.prefs import ChannelPrefs
from epicurus_core_app.push.service import NotifyResult

TENANT = "t1"


class _FakeNotifier:
    """Records every ``notify_effective`` call instead of touching a real push stack."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def notify_effective(
        self,
        tenant: str,
        *,
        effective: ChannelPrefs,
        category: str,
        title: str,
        body: str,
        deep_link: str | None = None,
        entity_ref: dict[str, Any] | None = None,
    ) -> NotifyResult:
        self.calls.append(
            {
                "tenant": tenant,
                "effective": effective,
                "category": category,
                "title": title,
                "body": body,
                "deep_link": deep_link,
                "entity_ref": entity_ref,
            }
        )
        return NotifyResult(outcome="sent", sent_count=1)


async def _subscriptions() -> EventSubscriptionStore:
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    store = EventSubscriptionStore(engine)
    await store.init()
    return store


def _event(
    *,
    module: str = "mail",
    event_type: str = "mail.received",
    tenant: str = TENANT,
    event_id: int = 1,
    causation_id: str | None = None,
    entity_ref: EntityRef | None = None,
) -> LoggedEvent:
    return LoggedEvent(
        id=event_id,
        tenant=tenant,
        module=module,
        type=event_type,
        occurred_at=datetime.now(UTC),
        received_at=datetime.now(UTC),
        dedup_key=f"k{event_id}",
        entity_ref=entity_ref,
        payload={},
        schema_version=1,
        causation_id=causation_id,
    )


# ── subscribe / skip ─────────────────────────────────────────────────────────────


async def test_an_unsubscribed_event_is_ignored() -> None:
    subs = await _subscriptions()
    push = _FakeNotifier()
    listener = EventAlertListener(subs, push, rate_cap_per_hour=0)
    await listener.on_event(_event())
    assert push.calls == []


async def test_a_subscribed_event_notifies() -> None:
    subs = await _subscriptions()
    await subs.set(
        TENANT,
        module="mail",
        event_type="mail.received",
        prefs=ChannelPrefs(push=True, center=True),
    )
    push = _FakeNotifier()
    listener = EventAlertListener(subs, push, rate_cap_per_hour=0)
    await listener.on_event(_event())
    assert len(push.calls) == 1
    assert push.calls[0]["tenant"] == TENANT
    assert push.calls[0]["effective"] == ChannelPrefs(push=True, center=True)
    assert push.calls[0]["category"] == "mail"


async def test_a_different_module_or_event_type_does_not_match() -> None:
    subs = await _subscriptions()
    await subs.set(
        TENANT,
        module="mail",
        event_type="mail.received",
        prefs=ChannelPrefs(push=True, center=True),
    )
    push = _FakeNotifier()
    listener = EventAlertListener(subs, push, rate_cap_per_hour=0)
    await listener.on_event(_event(module="tasks", event_type="tasks.due"))
    await listener.on_event(_event(module="mail", event_type="mail.sent"))
    assert push.calls == []


async def test_the_listener_is_tenant_scoped() -> None:
    subs = await _subscriptions()
    await subs.set(
        "other",
        module="mail",
        event_type="mail.received",
        prefs=ChannelPrefs(push=True, center=True),
    )
    push = _FakeNotifier()
    listener = EventAlertListener(subs, push, rate_cap_per_hour=0)
    await listener.on_event(_event(tenant=TENANT))
    assert push.calls == []


async def test_fires_regardless_of_causation_id() -> None:
    """Unlike AutomationMatcher's loop guard: no agent turn here, so nothing can spiral."""
    subs = await _subscriptions()
    await subs.set(
        TENANT,
        module="mail",
        event_type="mail.received",
        prefs=ChannelPrefs(push=True, center=True),
    )
    push = _FakeNotifier()
    listener = EventAlertListener(subs, push, rate_cap_per_hour=0)
    await listener.on_event(_event(causation_id="automation-1"))
    assert len(push.calls) == 1


# ── rendering ────────────────────────────────────────────────────────────────────


async def test_renders_module_and_type_with_no_entity_ref() -> None:
    subs = await _subscriptions()
    await subs.set(
        TENANT, module="echo", event_type="echo.pinged", prefs=ChannelPrefs(push=True, center=True)
    )
    push = _FakeNotifier()
    listener = EventAlertListener(subs, push, rate_cap_per_hour=0)
    await listener.on_event(_event(module="echo", event_type="echo.pinged"))
    assert push.calls[0]["title"] == "echo · echo.pinged"
    assert push.calls[0]["body"] == ""
    assert push.calls[0]["entity_ref"] is None


async def test_renders_the_entity_title_when_present() -> None:
    subs = await _subscriptions()
    await subs.set(
        TENANT,
        module="mail",
        event_type="mail.received",
        prefs=ChannelPrefs(push=True, center=True),
    )
    push = _FakeNotifier()
    listener = EventAlertListener(subs, push, rate_cap_per_hour=0)
    ref = EntityRef(ref_id="m1", module="mail", kind="message", title="Q3 invoice")
    await listener.on_event(_event(entity_ref=ref))
    assert push.calls[0]["title"] == "Q3 invoice"
    assert push.calls[0]["body"] == "mail · mail.received"
    assert push.calls[0]["entity_ref"] == ref.model_dump()


# ── rate cap ─────────────────────────────────────────────────────────────────────


async def test_rate_cap_blocks_after_the_limit() -> None:
    subs = await _subscriptions()
    await subs.set(
        TENANT,
        module="mail",
        event_type="mail.received",
        prefs=ChannelPrefs(push=True, center=True),
    )
    push = _FakeNotifier()
    listener = EventAlertListener(subs, push, rate_cap_per_hour=2)
    for i in range(4):
        await listener.on_event(_event(event_id=i))
    assert len(push.calls) == 2


async def test_zero_rate_cap_means_unlimited() -> None:
    subs = await _subscriptions()
    await subs.set(
        TENANT,
        module="mail",
        event_type="mail.received",
        prefs=ChannelPrefs(push=True, center=True),
    )
    push = _FakeNotifier()
    listener = EventAlertListener(subs, push, rate_cap_per_hour=0)
    for i in range(10):
        await listener.on_event(_event(event_id=i))
    assert len(push.calls) == 10


async def test_rate_cap_is_independent_per_subscription() -> None:
    """A chatty (mail, mail.received) must not spend a separate (tasks, tasks.due) budget."""
    subs = await _subscriptions()
    await subs.set(
        TENANT,
        module="mail",
        event_type="mail.received",
        prefs=ChannelPrefs(push=True, center=True),
    )
    await subs.set(
        TENANT, module="tasks", event_type="tasks.due", prefs=ChannelPrefs(push=True, center=True)
    )
    push = _FakeNotifier()
    listener = EventAlertListener(subs, push, rate_cap_per_hour=1)
    await listener.on_event(_event(module="mail", event_type="mail.received", event_id=1))
    await listener.on_event(_event(module="mail", event_type="mail.received", event_id=2))
    await listener.on_event(_event(module="tasks", event_type="tasks.due", event_id=3))
    assert len(push.calls) == 2  # the first mail event, plus the unrelated tasks event
    assert [c["category"] for c in push.calls] == ["mail", "tasks"]


async def test_rate_cap_is_tenant_scoped() -> None:
    subs = await _subscriptions()
    for tenant in ("a", "b"):
        await subs.set(
            tenant,
            module="mail",
            event_type="mail.received",
            prefs=ChannelPrefs(push=True, center=True),
        )
    push = _FakeNotifier()
    listener = EventAlertListener(subs, push, rate_cap_per_hour=1)
    await listener.on_event(_event(tenant="a", event_id=1))
    await listener.on_event(_event(tenant="b", event_id=2))
    assert len(push.calls) == 2  # separate tenants, separate budgets


# ── the listener's gates each log a distinct line (#797) ─────────────────────────
# The two gates upstream of PushService's own send path (whose gate lines are asserted in
# test_push_service.py). Gate 2 — no subscription for the (module, event_type) pair — was
# the issue's "bare return": the likeliest reason a notification never fires, and the most
# invisible.


class _CapturingLog:
    """Records every structured log call so tests can assert the exact decline lines."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def info(self, event: str, **kw: Any) -> None:
        self.events.append(("info", event, kw))

    def warning(self, event: str, **kw: Any) -> None:
        self.events.append(("warning", event, kw))

    @property
    def messages(self) -> list[str]:
        return [event for _, event, _ in self.events]


_LISTENER_GATE_LINES = {
    "no_subscription": "event alert declined: no subscription for this event",
    "rate_capped": "event alert rate cap reached",
}


@pytest.mark.parametrize("gate", sorted(_LISTENER_GATE_LINES))
async def test_every_listener_gate_logs_its_own_distinct_line(
    gate: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    logs = _CapturingLog()
    monkeypatch.setattr("epicurus_core_app.push.event_alerts.log", logs)
    subs = await _subscriptions()
    if gate == "rate_capped":
        await subs.set(
            TENANT,
            module="mail",
            event_type="mail.received",
            prefs=ChannelPrefs(push=True, center=True),
        )
    push = _FakeNotifier()
    listener = EventAlertListener(subs, push, rate_cap_per_hour=1)
    if gate == "rate_capped":
        await listener.on_event(_event(event_id=1))  # consumes the per-subscription budget
        logs.events.clear()

    await listener.on_event(_event(event_id=2))

    assert _LISTENER_GATE_LINES[gate] in logs.messages
    matched = next(kw for _lvl, event, kw in logs.events if event == _LISTENER_GATE_LINES[gate])
    assert matched["tenant"] == TENANT
    assert matched["module"] == "mail"
    assert matched["type"] == "mail.received"
    other = {line for name, line in _LISTENER_GATE_LINES.items() if name != gate}
    assert not other.intersection(logs.messages)


async def test_a_dispatched_alert_logs_no_decline_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """A subscribed, under-cap event notifies without logging either decline — so a decline
    line in the logs always means an alert actually declined."""
    logs = _CapturingLog()
    monkeypatch.setattr("epicurus_core_app.push.event_alerts.log", logs)
    subs = await _subscriptions()
    await subs.set(
        TENANT,
        module="mail",
        event_type="mail.received",
        prefs=ChannelPrefs(push=True, center=True),
    )
    push = _FakeNotifier()
    listener = EventAlertListener(subs, push, rate_cap_per_hour=0)
    await listener.on_event(_event())
    assert len(push.calls) == 1
    assert not set(_LISTENER_GATE_LINES.values()).intersection(logs.messages)
