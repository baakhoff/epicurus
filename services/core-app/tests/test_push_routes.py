"""Tests for the push routes: VAPID key, subscribe/unsubscribe, prefs, test-send, status."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pywebpush import WebPushException
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core import SecretError
from epicurus_core_app.notifications import NotificationStore
from epicurus_core_app.push.event_subscriptions import EventSubscriptionStore
from epicurus_core_app.push.prefs import PushPrefsStore
from epicurus_core_app.push.queue import PushQueueStore
from epicurus_core_app.push.routes import create_push_router
from epicurus_core_app.push.service import PushService
from epicurus_core_app.push.subscriptions import PushSubscriptionStore

TENANT = "test"


class _FakeSecretStore:
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
        pass


async def _utc() -> str:
    return "UTC"


def _engine() -> Any:
    return create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )


async def _app() -> FastAPI:
    subscriptions = PushSubscriptionStore(_engine())
    await subscriptions.init()
    prefs = PushPrefsStore(_engine())
    await prefs.init()
    queue = PushQueueStore(_engine())
    await queue.init()
    notifications = NotificationStore(_engine())
    await notifications.init()
    event_subscriptions = EventSubscriptionStore(_engine())
    await event_subscriptions.init()
    service = PushService(
        subscriptions=subscriptions,
        prefs=prefs,
        queue=queue,
        notifications=notifications,
        secrets=_FakeSecretStore(),  # type: ignore[arg-type]
        bus=_FakeEventBus(),  # type: ignore[arg-type]
        timezone=_utc,
        default_tenant=TENANT,
        vapid_subject="mailto:test@example.com",
        rate_cap_per_hour=30,
    )
    app = FastAPI()
    app.include_router(
        create_push_router(
            service,
            subscriptions=subscriptions,
            prefs=prefs,
            event_subscriptions=event_subscriptions,
            default_tenant=TENANT,
        )
    )
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_vapid_public_key_endpoint_returns_a_key() -> None:
    app = await _app()
    async with _client(app) as c:
        resp = await c.get("/platform/v1/push/vapid-public-key")
    assert resp.status_code == 200
    assert resp.json()["public_key"]


async def test_subscribe_then_list_round_trips() -> None:
    app = await _app()
    async with _client(app) as c:
        create = await c.post(
            "/platform/v1/push/subscriptions",
            json={
                "endpoint": "https://push.example/1",
                "p256dh": "p",
                "auth": "a",
                "device_label": "My Phone",
            },
        )
        listed = await c.get("/platform/v1/push/subscriptions")
    assert create.status_code == 200
    assert create.json()["device_label"] == "My Phone"
    assert len(listed.json()) == 1


async def test_subscribe_rejects_missing_fields() -> None:
    app = await _app()
    async with _client(app) as c:
        resp = await c.post(
            "/platform/v1/push/subscriptions", json={"endpoint": "", "p256dh": "p", "auth": "a"}
        )
    assert resp.status_code == 400


async def test_delete_subscription_removes_it_and_404s_unknown() -> None:
    app = await _app()
    async with _client(app) as c:
        create = await c.post(
            "/platform/v1/push/subscriptions",
            json={"endpoint": "e1", "p256dh": "p", "auth": "a"},
        )
        sub_id = create.json()["id"]
        delete = await c.delete(f"/platform/v1/push/subscriptions/{sub_id}")
        listed = await c.get("/platform/v1/push/subscriptions")
        missing = await c.delete(f"/platform/v1/push/subscriptions/{sub_id}")
    assert delete.status_code == 204
    assert listed.json() == []
    assert missing.status_code == 404


async def test_get_prefs_returns_every_known_category_defaulted() -> None:
    app = await _app()
    async with _client(app) as c:
        resp = await c.get("/platform/v1/push/prefs")
    body = resp.json()
    assert resp.status_code == 200
    assert set(body["categories"]) == {c["id"] for c in body["known_categories"]}
    assert all(v == {"push": True, "center": True} for v in body["categories"].values())
    assert body["quiet_hours_enabled"] is False


async def test_put_prefs_updates_one_category_without_touching_others() -> None:
    app = await _app()
    async with _client(app) as c:
        put = await c.put(
            "/platform/v1/push/prefs",
            json={"categories": {"mail": {"push": False, "center": True}}},
        )
        get = await c.get("/platform/v1/push/prefs")
    assert put.status_code == 200
    body = get.json()
    assert body["categories"]["mail"] == {"push": False, "center": True}
    assert body["categories"]["tasks"] == {"push": True, "center": True}  # untouched


async def test_put_prefs_updates_quiet_hours() -> None:
    app = await _app()
    async with _client(app) as c:
        put = await c.put(
            "/platform/v1/push/prefs",
            json={
                "quiet_hours_enabled": True,
                "quiet_hours_start": "23:00",
                "quiet_hours_end": "06:00",
            },
        )
    body = put.json()
    assert body["quiet_hours_enabled"] is True
    assert body["quiet_hours_start"] == "23:00"
    assert body["quiet_hours_end"] == "06:00"


async def test_put_prefs_rejects_a_malformed_quiet_hour() -> None:
    app = await _app()
    async with _client(app) as c:
        resp = await c.put(
            "/platform/v1/push/prefs",
            json={"quiet_hours_enabled": True, "quiet_hours_start": "not-a-time"},
        )
    assert resp.status_code == 400


async def test_put_prefs_partial_quiet_hours_update_preserves_the_rest() -> None:
    app = await _app()
    async with _client(app) as c:
        await c.put(
            "/platform/v1/push/prefs",
            json={
                "quiet_hours_enabled": True,
                "quiet_hours_start": "23:00",
                "quiet_hours_end": "06:00",
            },
        )
        # Flip only `enabled`; start/end should survive unchanged.
        put = await c.put("/platform/v1/push/prefs", json={"quiet_hours_enabled": False})
    body = put.json()
    assert body["quiet_hours_enabled"] is False
    assert body["quiet_hours_start"] == "23:00"
    assert body["quiet_hours_end"] == "06:00"


async def test_tenant_id_isolates_subscriptions_and_prefs() -> None:
    app = await _app()
    async with _client(app) as c:
        await c.post(
            "/platform/v1/push/subscriptions",
            params={"tenant_id": "b"},
            json={"endpoint": "e1", "p256dh": "p", "auth": "a"},
        )
        default_list = await c.get("/platform/v1/push/subscriptions")
        b_list = await c.get("/platform/v1/push/subscriptions", params={"tenant_id": "b"})
    assert default_list.json() == []
    assert len(b_list.json()) == 1


async def test_test_notification_endpoint_reports_no_devices_when_unsubscribed() -> None:
    app = await _app()
    async with _client(app) as c:
        resp = await c.post("/platform/v1/push/test", json={})
    assert resp.status_code == 200
    assert resp.json()["outcome"] == "skipped_no_devices"


async def test_test_notification_endpoint_sends_when_subscribed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", lambda **_kwargs: "ok")
    app = await _app()
    async with _client(app) as c:
        await c.post(
            "/platform/v1/push/subscriptions",
            json={"endpoint": "e1", "p256dh": "p", "auth": "a"},
        )
        resp = await c.post("/platform/v1/push/test", json={"category": "system"})
    assert resp.json() == {
        "outcome": "sent",
        "sent_count": 1,
        "pruned_count": 0,
        "failed_count": 0,
    }


async def test_test_notification_endpoint_reports_a_failed_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`outcome: "sent"` with zero delivered and a non-zero failure count is how an outright
    delivery failure surfaces (#797) — the settings card renders it as a failure."""

    def _boom(**_kwargs: Any) -> str:
        raise WebPushException("boom", response=SimpleNamespace(status_code=500))

    monkeypatch.setattr("epicurus_core_app.push.service.webpush", _boom)
    app = await _app()
    async with _client(app) as c:
        await c.post(
            "/platform/v1/push/subscriptions",
            json={"endpoint": "e1", "p256dh": "p", "auth": "a"},
        )
        resp = await c.post("/platform/v1/push/test", json={})
    body = resp.json()
    assert body["outcome"] == "sent"
    assert body["sent_count"] == 0
    assert body["failed_count"] == 1


# ── delivery status (#797) ────────────────────────────────────────────────────────


async def test_status_starts_with_no_devices_and_no_attempt() -> None:
    app = await _app()
    async with _client(app) as c:
        resp = await c.get("/platform/v1/push/status")
    assert resp.status_code == 200
    assert resp.json() == {"device_count": 0, "last_attempt": None}


async def test_status_reflects_the_last_send(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", lambda **_kwargs: "ok")
    app = await _app()
    async with _client(app) as c:
        await c.post(
            "/platform/v1/push/subscriptions",
            json={"endpoint": "e1", "p256dh": "p", "auth": "a"},
        )
        await c.post("/platform/v1/push/test", json={})
        resp = await c.get("/platform/v1/push/status")
    body = resp.json()
    assert body["device_count"] == 1
    attempt = body["last_attempt"]
    assert attempt is not None
    assert attempt["outcome"] == "sent"
    assert attempt["category"] == "system"
    assert attempt["sent_count"] == 1
    assert attempt["failed_count"] == 0
    assert attempt["at"]  # ISO timestamp, present


async def test_status_records_a_no_devices_attempt() -> None:
    """The misconfiguration the settings card warns about: push tried, nowhere to send."""
    app = await _app()
    async with _client(app) as c:
        await c.post("/platform/v1/push/test", json={})
        resp = await c.get("/platform/v1/push/status")
    body = resp.json()
    assert body["device_count"] == 0
    assert body["last_attempt"]["outcome"] == "skipped_no_devices"


async def test_status_is_tenant_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("epicurus_core_app.push.service.webpush", lambda **_kwargs: "ok")
    app = await _app()
    async with _client(app) as c:
        await c.post(
            "/platform/v1/push/subscriptions",
            params={"tenant_id": "b"},
            json={"endpoint": "e1", "p256dh": "p", "auth": "a"},
        )
        await c.post("/platform/v1/push/test", params={"tenant_id": "b"}, json={})
        default_status = await c.get("/platform/v1/push/status")
        b_status = await c.get("/platform/v1/push/status", params={"tenant_id": "b"})
    assert default_status.json() == {"device_count": 0, "last_attempt": None}
    assert b_status.json()["device_count"] == 1
    assert b_status.json()["last_attempt"]["outcome"] == "sent"


# ── event subscriptions (#732) ────────────────────────────────────────────────────


async def test_list_event_subscriptions_starts_empty() -> None:
    app = await _app()
    async with _client(app) as c:
        resp = await c.get("/platform/v1/push/event-subscriptions")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_put_then_list_event_subscription_round_trips() -> None:
    app = await _app()
    async with _client(app) as c:
        put = await c.put(
            "/platform/v1/push/event-subscriptions",
            json={"module": "mail", "event_type": "mail.received", "push": True, "center": False},
        )
        listed = await c.get("/platform/v1/push/event-subscriptions")
    assert put.status_code == 200
    assert put.json() == {
        "module": "mail",
        "event_type": "mail.received",
        "push": True,
        "center": False,
    }
    assert listed.json() == [put.json()]


async def test_put_event_subscription_rejects_blank_fields() -> None:
    app = await _app()
    async with _client(app) as c:
        resp = await c.put(
            "/platform/v1/push/event-subscriptions",
            json={"module": "", "event_type": "mail.received", "push": True, "center": True},
        )
    assert resp.status_code == 400


async def test_put_event_subscription_with_both_off_removes_it_from_the_list() -> None:
    app = await _app()
    async with _client(app) as c:
        await c.put(
            "/platform/v1/push/event-subscriptions",
            json={"module": "mail", "event_type": "mail.received", "push": True, "center": True},
        )
        await c.put(
            "/platform/v1/push/event-subscriptions",
            json={"module": "mail", "event_type": "mail.received", "push": False, "center": False},
        )
        listed = await c.get("/platform/v1/push/event-subscriptions")
    assert listed.json() == []


async def test_event_subscriptions_are_tenant_scoped() -> None:
    app = await _app()
    async with _client(app) as c:
        await c.put(
            "/platform/v1/push/event-subscriptions",
            params={"tenant_id": "b"},
            json={"module": "mail", "event_type": "mail.received", "push": True, "center": True},
        )
        default_list = await c.get("/platform/v1/push/event-subscriptions")
        b_list = await c.get("/platform/v1/push/event-subscriptions", params={"tenant_id": "b"})
    assert default_list.json() == []
    assert len(b_list.json()) == 1
