"""Unit tests for PushService.notify/send_digest — prefs routing, quiet hours, rate caps,
delivery, and Gone-subscription pruning. ``pywebpush.webpush`` is monkeypatched (a real
send needs a live push service); the send-path *decisions* are what's under test.
"""

from __future__ import annotations

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


async def test_notify_skips_the_center_row_when_center_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture()
    await fx.subscriptions.create_or_update(tenant=TENANT, endpoint="e1", p256dh="p", auth="a")
    await fx.prefs.set_categories(TENANT, {"mail": ChannelPrefs(push=True, center=False)})
    await fx.service.notify(TENANT, category="mail", title="t", body="b")
    assert await fx.notifications.list(TENANT) == []


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


async def test_notify_uses_the_automation_overrides_center_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _webpush_ok)
    fx = await _fixture()
    await fx.prefs.set_categories(TENANT, {"automation": ChannelPrefs(push=True, center=True)})
    await fx.prefs.set_automation_override(TENANT, "auto-1", ChannelPrefs(push=True, center=False))
    await fx.service.notify(
        TENANT, category="automation", title="t", body="b", automation_id="auto-1"
    )
    assert await fx.notifications.list(TENANT) == []  # override's center=False wins


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
    assert len(await fx.subscriptions.list(TENANT)) == 1  # a transient 500 is not Gone


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
