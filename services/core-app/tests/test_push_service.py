"""Unit tests for PushService.notify/send_digest — prefs routing, quiet hours, rate caps,
delivery, Gone-subscription pruning, the unconditional center write, the per-gate decline
logs, and the last-attempt diagnostics (#797). ``pywebpush.webpush`` is monkeypatched (a
real send needs a live push service); the send-path *decisions* are what's under test.
The module logger is monkeypatched with a recording double where log lines are asserted —
deterministic regardless of global structlog configuration or logger caching.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from pywebpush import WebPushException
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core import SecretError
from epicurus_core_app.notifications import NotificationStore
from epicurus_core_app.push.prefs import ChannelPrefs, PushPrefsStore
from epicurus_core_app.push.queue import PushQueueStore, QueuedPush
from epicurus_core_app.push.service import PushService
from epicurus_core_app.push.subscriptions import PushSubscriptionStore

TENANT = "t1"


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
    def __init__(self) -> None:
        self.published: list[tuple[str, Any, str | None]] = []
        self._fail = False

    def fail_next(self) -> None:
        self._fail = True

    async def publish(self, subject: str, data: Any, tenant_id: str | None = None) -> None:
        if self._fail:
            raise RuntimeError("nats is down")
        self.published.append((subject, data, tenant_id))


async def _utc() -> str:
    return "UTC"


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


def _queued(category: str, title: str) -> QueuedPush:
    return QueuedPush(
        tenant=TENANT,
        category=category,
        title=title,
        body="b",
        deep_link=None,
        entity_ref=None,
        queued_at=datetime.now(UTC),
    )


class _Fixture:
    def __init__(
        self,
        service: PushService,
        subscriptions: PushSubscriptionStore,
        prefs: PushPrefsStore,
        queue: PushQueueStore,
        notifications: NotificationStore,
        bus: _FakeEventBus,
    ) -> None:
        self.service = service
        self.subscriptions = subscriptions
        self.prefs = prefs
        self.queue = queue
        self.notifications = notifications
        self.bus = bus


async def _fixture(*, rate_cap_per_hour: int = 30, timezone: Any = _utc) -> _Fixture:
    def _engine() -> Any:
        return create_async_engine(
            "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
        )

    subscriptions = PushSubscriptionStore(_engine())
    await subscriptions.init()
    prefs = PushPrefsStore(_engine())
    await prefs.init()
    queue = PushQueueStore(_engine())
    await queue.init()
    notifications = NotificationStore(_engine())
    await notifications.init()
    bus = _FakeEventBus()
    service = PushService(
        subscriptions=subscriptions,
        prefs=prefs,
        queue=queue,
        notifications=notifications,
        secrets=_FakeSecretStore(),  # type: ignore[arg-type]
        bus=bus,  # type: ignore[arg-type]
        timezone=timezone,
        default_tenant=TENANT,
        vapid_subject="mailto:test@example.com",
        rate_cap_per_hour=rate_cap_per_hour,
    )
    return _Fixture(service, subscriptions, prefs, queue, notifications, bus)


def _webpush_ok(**_kwargs: Any) -> str:
    return "ok"


def _webpush_gone(**_kwargs: Any) -> str:
    raise WebPushException("Gone", response=SimpleNamespace(status_code=410))


def _webpush_server_error(**_kwargs: Any) -> str:
    raise WebPushException("boom", response=SimpleNamespace(status_code=500))


# ── get_vapid_public_key ─────────────────────────────────────────────────────────


async def test_vapid_key_is_generated_once_and_reused() -> None:
    fx = await _fixture()
    first = await fx.service.get_vapid_public_key(TENANT)
    second = await fx.service.get_vapid_public_key(TENANT)
    assert first == second


async def test_vapid_key_is_tenant_scoped() -> None:
    fx = await _fixture()
    a = await fx.service.get_vapid_public_key("a")
    b = await fx.service.get_vapid_public_key("b")
    assert a != b


# ── notify: prefs routing ────────────────────────────────────────────────────────


async def test_notify_skips_when_category_push_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture()
    await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    await fx.prefs.set_categories(TENANT, {"mail": ChannelPrefs(push=False, center=True)})
    result = await fx.service.notify(TENANT, category="mail", title="t", body="b")
    assert result.outcome == "skipped_disabled"
    assert result.sent_count == 0


async def test_notify_prefers_automation_override_over_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture()
    await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    await fx.prefs.set_categories(TENANT, {"automation": ChannelPrefs(push=True, center=True)})
    await fx.prefs.set_automation_override(TENANT, "auto-1", ChannelPrefs(push=False, center=True))
    result = await fx.service.notify(
        TENANT, category="automation", title="t", body="b", automation_id="auto-1"
    )
    assert result.outcome == "skipped_disabled"


async def test_notify_delivers_when_no_prefs_are_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default posture is on/on — an unconfigured category still delivers."""
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture()
    await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    result = await fx.service.notify(TENANT, category="mail", title="t", body="b")
    assert result.outcome == "sent"
    assert result.sent_count == 1


# ── notify: notification center (#671) ────────────────────────────────────────────


async def test_notify_records_a_center_row_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture()
    await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    ref = {"ref_id": "e1", "module": "mail", "kind": "thread", "title": "Hello"}
    await fx.service.notify(
        TENANT,
        category="mail",
        title="New mail",
        body="b",
        deep_link="/m/mail/e1",
        entity_ref=ref,
    )
    rows = await fx.notifications.list(TENANT)
    assert len(rows) == 1
    assert rows[0].category == "mail"
    assert rows[0].title == "New mail"
    assert rows[0].deep_link == "/m/mail/e1"
    assert rows[0].entity_ref == ref
    assert rows[0].read_at is None


async def test_notify_records_the_center_row_even_when_center_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#797: the center is a superset of push — a stored `center: false` (a pre-#797 pref,
    or one sent by an old client) no longer suppresses the durable row."""
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture()
    await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    await fx.prefs.set_categories(TENANT, {"mail": ChannelPrefs(push=True, center=False)})
    await fx.service.notify(TENANT, category="mail", title="t", body="b")
    assert len(await fx.notifications.list(TENANT)) == 1


async def test_notify_records_the_center_row_even_when_both_channels_are_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even `{push: false, center: false}` keeps the durable record (#797) — the only way to
    not record an alert is for it not to fire (e.g. no event subscription upstream)."""
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture()
    await fx.prefs.set_categories(TENANT, {"mail": ChannelPrefs(push=False, center=False)})
    result = await fx.service.notify(TENANT, category="mail", title="t", body="b")
    assert result.outcome == "skipped_disabled"
    assert len(await fx.notifications.list(TENANT)) == 1


async def test_notify_records_the_center_row_even_when_push_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`push` and `center` are independent toggles — one being off must not affect the other."""
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture()
    await fx.prefs.set_categories(TENANT, {"mail": ChannelPrefs(push=False, center=True)})
    result = await fx.service.notify(TENANT, category="mail", title="t", body="b")
    assert result.outcome == "skipped_disabled"
    rows = await fx.notifications.list(TENANT)
    assert len(rows) == 1


async def test_notify_records_the_center_row_immediately_during_quiet_hours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance criterion (#671): a quiet-hours-suppressed push still appears in the
    center immediately — it does not wait for the digest to flush."""
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture()
    await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    await fx.prefs.set_quiet_hours(TENANT, enabled=True, start="00:00", end="23:59")
    result = await fx.service.notify(TENANT, category="mail", title="t", body="b")
    assert result.outcome == "queued"
    rows = await fx.notifications.list(TENANT)
    assert len(rows) == 1  # recorded immediately, not deferred alongside the push digest


async def test_notify_records_the_center_row_even_when_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture(rate_cap_per_hour=1)
    await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    await fx.service.notify(TENANT, category="mail", title="first", body="b")
    result = await fx.service.notify(TENANT, category="mail", title="second", body="b")
    assert result.outcome == "skipped_rate_limited"
    rows = await fx.notifications.list(TENANT)
    assert len(rows) == 2  # both recorded, even though only the first was actually pushed


async def test_notify_ignores_the_automation_overrides_center_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An automation override can still silence *push* per automation, but its `center` half
    is vestigial (#797) — the durable row is always written."""
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture()
    await fx.prefs.set_categories(TENANT, {"automation": ChannelPrefs(push=True, center=True)})
    await fx.prefs.set_automation_override(TENANT, "auto-1", ChannelPrefs(push=True, center=False))
    await fx.service.notify(
        TENANT, category="automation", title="t", body="b", automation_id="auto-1"
    )
    assert len(await fx.notifications.list(TENANT)) == 1


async def test_notify_records_the_center_row_when_delivery_fails_outright(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The issue's motivating case (#797): every device errors, nothing is delivered — the
    center row must already exist, so the notification is never simply gone."""
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_server_error)
    fx = await _fixture()
    await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    result = await fx.service.notify(TENANT, category="mail", title="t", body="b")
    assert result.sent_count == 0
    assert result.failed_count == 1
    rows = await fx.notifications.list(TENANT)
    assert len(rows) == 1
    assert result.notification_id == rows[0].id


# ── notify: NotifyResult.notification_id (#723) ────────────────────────────────────
# A caller (the automations push sink) needs the center row's id back to build an EntityRef
# pointing at what was recorded, without a second, racy lookup.


async def test_notify_returns_the_center_rows_id_when_center_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture()
    result = await fx.service.notify(TENANT, category="mail", title="t", body="b")
    rows = await fx.notifications.list(TENANT)
    assert result.notification_id == rows[0].id


async def test_notify_returns_the_notification_id_even_when_center_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#797: a row is always written, so the id always comes back — no caller branch needed."""
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture()
    await fx.prefs.set_categories(TENANT, {"mail": ChannelPrefs(push=True, center=False)})
    result = await fx.service.notify(TENANT, category="mail", title="t", body="b")
    assert result.notification_id is not None


async def test_notify_carries_the_notification_id_through_every_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`notification_id` is set as soon as the center row is written — before push is ever
    resolved — so it rides along with `queued`/`skipped_rate_limited`/`skipped_disabled`
    exactly as it does with `sent`, never only the happy path."""
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture()
    await fx.prefs.set_quiet_hours(TENANT, enabled=True, start="00:00", end="23:59")
    result = await fx.service.notify(TENANT, category="mail", title="t", body="b")
    assert result.outcome == "queued"
    assert result.notification_id is not None


async def test_send_digest_never_sets_a_notification_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """A digest summarizes several already-recorded rows — it has no single row of its own."""
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture()
    await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    result = await fx.service.send_digest(TENANT, [_queued("mail", "New mail")])
    assert result.notification_id is None


# ── notify: wire-payload truncation (#723) ──────────────────────────────────────────


async def test_notify_truncates_the_outgoing_payload_body_but_not_the_center_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def _record(**kwargs: Any) -> str:
        calls.append(kwargs)
        return "ok"

    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _record)
    fx = await _fixture()
    await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    long_body = "x" * 5000
    await fx.service.notify(TENANT, category="mail", title="t", body=long_body)
    sent_payload = json.loads(calls[0]["data"])
    assert len(sent_payload["body"]) < len(long_body)
    assert sent_payload["body"].endswith("…")
    rows = await fx.notifications.list(TENANT)
    assert rows[0].body == long_body  # the durable record is never shortened


async def test_notify_does_not_touch_a_body_under_the_truncation_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def _record(**kwargs: Any) -> str:
        calls.append(kwargs)
        return "ok"

    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _record)
    fx = await _fixture()
    await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    await fx.service.notify(TENANT, category="mail", title="t", body="a short body")
    sent_payload = json.loads(calls[0]["data"])
    assert sent_payload["body"] == "a short body"


# ── notify: quiet hours ──────────────────────────────────────────────────────────


async def test_notify_queues_instead_of_sending_during_quiet_hours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture()
    await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    # A window covering the entire day (00:00-23:59) — deterministically "quiet" regardless
    # of when the test runs, without needing to freeze the clock.
    await fx.prefs.set_quiet_hours(TENANT, enabled=True, start="00:00", end="23:59")
    result = await fx.service.notify(TENANT, category="mail", title="t", body="b")
    assert result.outcome == "queued"
    items = await fx.queue.list_for_tenant(TENANT)
    assert len(items) == 1
    assert items[0].title == "t"


async def test_notify_delivers_immediately_once_quiet_hours_are_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture()
    await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    await fx.prefs.set_quiet_hours(TENANT, enabled=False, start="00:00", end="23:59")
    result = await fx.service.notify(TENANT, category="mail", title="t", body="b")
    assert result.outcome == "sent"


# ── notify: rate cap ──────────────────────────────────────────────────────────────


async def test_notify_rate_limits_after_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture(rate_cap_per_hour=2)
    await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    first = await fx.service.notify(TENANT, category="mail", title="t", body="b")
    second = await fx.service.notify(TENANT, category="mail", title="t", body="b")
    third = await fx.service.notify(TENANT, category="mail", title="t", body="b")
    assert [first.outcome, second.outcome] == ["sent", "sent"]
    assert third.outcome == "skipped_rate_limited"


async def test_rate_cap_is_tenant_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture(rate_cap_per_hour=1)
    await fx.subscriptions.create_or_update(tenant="a", endpoint="e1", p256dh="p", auth="a")
    await fx.subscriptions.create_or_update(tenant="b", endpoint="e2", p256dh="p", auth="a")
    a_result = await fx.service.notify("a", category="mail", title="t", body="b")
    b_result = await fx.service.notify("b", category="mail", title="t", body="b")
    assert a_result.outcome == "sent"
    assert b_result.outcome == "sent"  # a separate tenant's cap, unaffected by "a"'s usage


async def test_zero_rate_cap_means_unlimited(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture(rate_cap_per_hour=0)
    await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    for _ in range(5):
        result = await fx.service.notify(TENANT, category="mail", title="t", body="b")
        assert result.outcome == "sent"


# ── notify: delivery + pruning ────────────────────────────────────────────────────


async def test_notify_with_no_subscriptions_is_skipped_no_devices() -> None:
    fx = await _fixture()
    result = await fx.service.notify(TENANT, category="mail", title="t", body="b")
    assert result.outcome == "skipped_no_devices"


async def test_notify_sends_to_every_subscribed_device(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def _record(**kwargs: Any) -> str:
        calls.append(kwargs)
        return "ok"

    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _record)
    fx = await _fixture()
    await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p1", auth="a1")
    await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="e2", p256dh="p2", auth="a2")
    result = await fx.service.notify(TENANT, category="mail", title="Hi", body="there")
    assert result.outcome == "sent"
    assert result.sent_count == 2
    assert len(calls) == 2
    endpoints = {c["subscription_info"]["endpoint"] for c in calls}
    assert endpoints == {"e1", "e2"}
    assert '"title": "Hi"' in calls[0]["data"]


async def test_a_gone_subscription_is_pruned_and_others_still_receive_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _mixed(**kwargs: Any) -> str:
        if kwargs["subscription_info"]["endpoint"] == "dead":
            return _webpush_gone(**kwargs)
        return "ok"

    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _mixed)
    fx = await _fixture()
    await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="dead", p256dh="p", auth="a")
    await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="alive", p256dh="p", auth="a")
    result = await fx.service.notify(TENANT, category="mail", title="t", body="b")
    assert result.outcome == "sent"
    assert result.sent_count == 1
    assert result.pruned_count == 1
    remaining = await fx.subscriptions.list(TENANT)
    assert [s.endpoint for s in remaining] == ["alive"]


async def test_a_non_gone_error_does_not_prune_the_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_server_error)
    fx = await _fixture()
    await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    result = await fx.service.notify(TENANT, category="mail", title="t", body="b")
    assert result.sent_count == 0
    assert result.pruned_count == 0
    assert result.failed_count == 1  # counted as a failure, not silently absorbed (#797)
    assert len(await fx.subscriptions.list(TENANT)) == 1  # a transient 500 is not Gone


# ── the delivery gates each log a distinct line (#797) ────────────────────────────
# A push crosses several independent gates and used to die quietly at whichever was closed.
# Every declined/deferred path must log an identifiable line so an operator can tell
# "working as configured" from "broken" — parametrized over every gate this service owns
# (the event-alert listener's two upstream gates are asserted in test_event_alerts.py).

_GATE_LINES: dict[str, str] = {
    "push_disabled": "push skipped: push disabled for this notification",
    "quiet_hours_queued": "push queued for quiet-hours digest",
    "tenant_rate_capped": "push rate cap reached; delivery skipped",
    "no_devices": "push skipped: no registered devices",
    "delivery_failed": "push send failed",
    "dead_subscription_pruned": "pruned dead push subscription",
}


@pytest.mark.parametrize("gate", sorted(_GATE_LINES))
async def test_every_push_gate_logs_its_own_distinct_line(
    gate: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    logs = _CapturingLog()
    monkeypatch.setattr("epicurus_core_app.push.service.log", logs)
    monkeypatch.setattr(
        "epicurus_core_app.push.service.webpush",
        {
            "delivery_failed": _webpush_server_error,
            "dead_subscription_pruned": _webpush_gone,
        }.get(gate, _webpush_ok),
    )
    fx = await _fixture(rate_cap_per_hour=1 if gate == "tenant_rate_capped" else 30)
    if gate != "no_devices":
        await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    if gate == "push_disabled":
        await fx.prefs.set_categories(TENANT, {"mail": ChannelPrefs(push=False, center=True)})
    if gate == "quiet_hours_queued":
        await fx.prefs.set_quiet_hours(TENANT, enabled=True, start="00:00", end="23:59")
    if gate == "tenant_rate_capped":
        await fx.service.notify(TENANT, category="mail", title="warm-up", body="b")
        logs.events.clear()  # only the capped call's lines are under test

    await fx.service.notify(TENANT, category="mail", title="t", body="b")

    assert _GATE_LINES[gate] in logs.messages
    matched = next(kw for _level, event, kw in logs.events if event == _GATE_LINES[gate])
    assert matched["tenant"] == TENANT  # attributable, not just present
    other_lines = {line for name, line in _GATE_LINES.items() if name != gate}
    assert not other_lines.intersection(logs.messages)  # distinct: no cross-talk between gates


async def test_a_clean_send_logs_no_decline_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """The inverse guard: a healthy delivery stays quiet, so a decline line always means
    something actually declined."""
    logs = _CapturingLog()
    monkeypatch.setattr("epicurus_core_app.push.service.log", logs)
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture()
    await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    result = await fx.service.notify(TENANT, category="mail", title="t", body="b")
    assert result.outcome == "sent"
    assert not set(_GATE_LINES.values()).intersection(logs.messages)


# ── last-attempt diagnostics (#797) ───────────────────────────────────────────────
# GET /push/status surfaces the most recent delivery attempt so the settings card can answer
# "when was a push last tried, and did it succeed" without the operator reading logs.


async def test_last_attempt_is_none_until_a_push_is_attempted() -> None:
    fx = await _fixture()
    assert fx.service.last_attempt(TENANT) is None


async def test_a_send_records_the_last_attempt_with_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture()
    await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    await fx.service.notify(TENANT, category="mail", title="t", body="b")
    attempt = fx.service.last_attempt(TENANT)
    assert attempt is not None
    assert attempt.outcome == "sent"
    assert attempt.category == "mail"
    assert attempt.sent_count == 1
    assert attempt.failed_count == 0


async def test_a_failed_delivery_records_the_attempt_with_its_failure_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_server_error)
    fx = await _fixture()
    await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    await fx.service.notify(TENANT, category="mail", title="t", body="b")
    attempt = fx.service.last_attempt(TENANT)
    assert attempt is not None
    assert attempt.outcome == "sent"
    assert attempt.sent_count == 0
    assert attempt.failed_count == 1  # "sent" outcome, zero delivered — the status can say so


@pytest.mark.parametrize(
    ("gate", "expected_outcome"),
    [
        ("quiet_hours_queued", "queued"),
        ("tenant_rate_capped", "skipped_rate_limited"),
        ("no_devices", "skipped_no_devices"),
    ],
)
async def test_every_non_delivery_attempt_is_still_recorded(
    gate: str, expected_outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture(rate_cap_per_hour=1 if gate == "tenant_rate_capped" else 30)
    if gate != "no_devices":
        await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    if gate == "quiet_hours_queued":
        await fx.prefs.set_quiet_hours(TENANT, enabled=True, start="00:00", end="23:59")
    if gate == "tenant_rate_capped":
        await fx.service.notify(TENANT, category="mail", title="warm-up", body="b")
    await fx.service.notify(TENANT, category="mail", title="t", body="b")
    attempt = fx.service.last_attempt(TENANT)
    assert attempt is not None
    assert attempt.outcome == expected_outcome


async def test_a_disabled_push_never_records_an_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Push off means nothing was attempted — recording it would overwrite the diagnostics
    of the last real attempt with a non-event."""
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture()
    await fx.prefs.set_categories(TENANT, {"mail": ChannelPrefs(push=False, center=True)})
    await fx.service.notify(TENANT, category="mail", title="t", body="b")
    assert fx.service.last_attempt(TENANT) is None


async def test_last_attempt_is_tenant_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture()
    await fx.subscriptions.create_or_update(tenant="a", endpoint="e1", p256dh="p", auth="a")
    await fx.service.notify("a", category="mail", title="t", body="b")
    assert fx.service.last_attempt("a") is not None
    assert fx.service.last_attempt("b") is None


async def test_notify_emits_a_best_effort_usage_event(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture()
    await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    await fx.service.notify(TENANT, category="mail", title="t", body="b")
    assert len(fx.bus.published) == 1
    subject, data, tenant_id = fx.bus.published[0]
    assert subject == "push.sent"
    assert tenant_id == TENANT
    assert data["category"] == "mail"
    assert data["device_count"] == 1


async def test_notify_succeeds_even_if_the_usage_event_publish_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture()
    await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    fx.bus.fail_next()
    result = await fx.service.notify(TENANT, category="mail", title="t", body="b")
    assert result.outcome == "sent"  # usage-event failure never breaks the send


# ── send_digest ───────────────────────────────────────────────────────────────────


async def test_send_digest_summarizes_the_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def _record(**kwargs: Any) -> str:
        calls.append(kwargs)
        return "ok"

    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _record)
    fx = await _fixture()
    await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    items = [_queued("mail", "New mail"), _queued("tasks", "Task due")]
    result = await fx.service.send_digest(TENANT, items)
    assert result.outcome == "sent"
    assert len(calls) == 1
    assert '"title": "2 notifications while you were quiet"' in calls[0]["data"]
    assert '"deep_link": "/notifications"' in calls[0]["data"]


async def test_send_digest_uses_singular_wording_for_one_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def _record(**kwargs: Any) -> str:
        calls.append(kwargs)
        return "ok"

    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _record)
    fx = await _fixture()
    await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    result = await fx.service.send_digest(TENANT, [_queued("mail", "New mail")])
    assert result.outcome == "sent"
    assert '"title": "1 notification while you were quiet"' in calls[0]["data"]


async def test_send_digest_with_no_subscriptions_is_skipped_no_devices() -> None:
    fx = await _fixture()
    result = await fx.service.send_digest(TENANT, [_queued("mail", "New mail")])
    assert result.outcome == "skipped_no_devices"


async def test_send_digest_respects_the_rate_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture(rate_cap_per_hour=1)
    await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    await fx.service.notify(TENANT, category="mail", title="t", body="b")  # consumes the cap
    result = await fx.service.send_digest(TENANT, [_queued("mail", "t")])
    assert result.outcome == "skipped_rate_limited"


# ── notify_effective (#732) ───────────────────────────────────────────────────────
# The event-alerts send path: the caller has already resolved push/center (from a per-event
# subscription, not a category), so there is no PushPrefs.categories/automation_overrides
# lookup to exercise here — these tests confirm it shares notify()'s delivery mechanics
# (center write, quiet hours, tenant-wide rate cap) exactly, via the same `_deliver` path.


async def test_notify_effective_uses_the_passed_in_prefs_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Category prefs must play no role — an event-alert subscription is not a category."""
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture()
    await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    await fx.prefs.set_categories(TENANT, {"mail": ChannelPrefs(push=False, center=False)})
    result = await fx.service.notify_effective(
        TENANT,
        effective=ChannelPrefs(push=True, center=True),
        category="mail",
        title="t",
        body="b",
    )
    assert result.outcome == "sent"  # the category's push=False never applies


async def test_notify_effective_skips_push_when_the_passed_in_prefs_say_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture()
    await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    result = await fx.service.notify_effective(
        TENANT,
        effective=ChannelPrefs(push=False, center=True),
        category="mail",
        title="t",
        body="b",
    )
    assert result.outcome == "skipped_disabled"
    rows = await fx.notifications.list(TENANT)
    assert len(rows) == 1  # center is independent, and was on


async def test_notify_effective_records_the_center_row_even_when_center_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#797 applies to the event-alerts path identically: firing at all is what records."""
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture()
    result = await fx.service.notify_effective(
        TENANT,
        effective=ChannelPrefs(push=False, center=False),
        category="mail",
        title="t",
        body="b",
    )
    assert result.notification_id is not None
    assert len(await fx.notifications.list(TENANT)) == 1


async def test_notify_effective_queues_during_quiet_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture()
    await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    await fx.prefs.set_quiet_hours(TENANT, enabled=True, start="00:00", end="23:59")
    result = await fx.service.notify_effective(
        TENANT, effective=ChannelPrefs(push=True, center=True), category="mail", title="t", body="b"
    )
    assert result.outcome == "queued"
    assert len(await fx.queue.list_for_tenant(TENANT)) == 1


async def test_notify_effective_respects_the_tenant_wide_rate_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-subscription cap lives in EventAlertListener; PushService still enforces its
    own tenant-wide cap underneath, same as any other caller of the send path."""
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture(rate_cap_per_hour=1)
    await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    effective = ChannelPrefs(push=True, center=True)
    first = await fx.service.notify_effective(
        TENANT, effective=effective, category="mail", title="t", body="b"
    )
    second = await fx.service.notify_effective(
        TENANT, effective=effective, category="mail", title="t", body="b"
    )
    assert first.outcome == "sent"
    assert second.outcome == "skipped_rate_limited"


async def test_notify_effective_passes_through_entity_ref_and_deep_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture()
    ref = {"ref_id": "m1", "module": "mail", "kind": "message", "title": "Q3 invoice"}
    await fx.service.notify_effective(
        TENANT,
        effective=ChannelPrefs(push=False, center=True),
        category="mail",
        title="Q3 invoice",
        body="mail.received",
        deep_link="/m/mail/m1",
        entity_ref=ref,
    )
    rows = await fx.notifications.list(TENANT)
    assert rows[0].entity_ref == ref
    assert rows[0].deep_link == "/m/mail/m1"
