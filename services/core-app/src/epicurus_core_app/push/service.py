"""The push send path — a core-internal contract, not an HTTP route (#670, ADR-0102).

:meth:`PushService.notify` is what a future caller (the automations engine's push sink;
a core-originated system notice) calls directly in-process — ``notify(tenant, category=...,
title=..., body=...)``. Nothing in this PR calls it except the settings UI's "send test
notification" button (``push/routes.py``), since no event source exists yet to trigger a
real one; see docs/reference/notifications.md for the signature as the documented contract
future callers code against.

Every call first records a notification-center row (``epicurus_core_app.notifications``) if
the category/automation's ``center`` toggle is on — regardless of what push delivery does
below (#671, ADR-0102 §4). Push delivery then resolves, in order: (1) the effective push
toggle — off skips delivery entirely; (2) quiet hours in the tenant's timezone (ADR-0039) —
queues for a digest rather than sending; (3) an in-memory per-tenant rate cap — single-instance v1,
the same disposable-cache trade ADR-0055's live-run registry makes, not backed by a table
since losing counts on a restart just under-limits for one window, never over-limits.
Delivery itself fans out to every device via VAPID-signed webpush (RFC 8291/8292), pruning
any subscription the push service reports Gone (404/410) — a dead endpoint is expected
churn (uninstalled PWA, cleared site data), not an error.
"""

from __future__ import annotations

import asyncio
import json
import time as _time
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pywebpush import WebPushException, webpush

from epicurus_core import EventBus, SecretError, SecretStore, get_logger
from epicurus_core_app.notifications import NotificationStore
from epicurus_core_app.push.prefs import PushPrefsStore, is_quiet_now
from epicurus_core_app.push.queue import PushQueueStore, QueuedPush
from epicurus_core_app.push.subscriptions import PushSubscriptionStore
from epicurus_core_app.push.vapid import generate_vapid_keypair, load_vapid_signer
from epicurus_core_app.scheduling import TimezoneProvider

__all__ = ["NotifyResult", "PushService"]

log = get_logger("epicurus_core_app.push.service")

_VAPID_SECRET_PATH = "push/vapid"
# A push the recipient device is offline for is held by the push service and delivered on
# reconnect for up to this long, then dropped — long enough that "offline overnight" still
# arrives, short enough that a very stale device doesn't get a burst of week-old pushes.
_TTL_SECONDS = 24 * 60 * 60

Outcome = Literal[
    "sent", "queued", "skipped_disabled", "skipped_rate_limited", "skipped_no_devices"
]


@dataclass(frozen=True)
class NotifyResult:
    """What :meth:`PushService.notify` (or ``send_digest``) actually did."""

    outcome: Outcome
    sent_count: int = 0
    pruned_count: int = 0


class PushService:
    """Resolves prefs/quiet-hours/rate-caps and fans a notification out to a tenant's devices."""

    def __init__(
        self,
        *,
        subscriptions: PushSubscriptionStore,
        prefs: PushPrefsStore,
        queue: PushQueueStore,
        notifications: NotificationStore,
        secrets: SecretStore,
        bus: EventBus,
        timezone: TimezoneProvider,
        default_tenant: str,
        vapid_subject: str,
        rate_cap_per_hour: int,
    ) -> None:
        self._subscriptions = subscriptions
        self.prefs = prefs
        self._queue = queue
        self._notifications = notifications
        self._secrets = secrets
        self._bus = bus
        self._timezone = timezone
        self._default_tenant = default_tenant
        self._vapid_subject = vapid_subject
        self._rate_cap_per_hour = rate_cap_per_hour
        # tenant -> (window_start_monotonic, count). In-memory, single-instance v1 (see the
        # module docstring) — never persisted, so a restart resets every tenant's window.
        self._rate_windows: dict[str, tuple[float, int]] = {}

    async def get_vapid_public_key(self, tenant: str) -> str:
        """The tenant's ``applicationServerKey`` bytes, base64url — for the browser to subscribe."""
        _, public_key = await self._vapid_keypair(tenant)
        return public_key

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
        """Record the notification-center row (if `center` is on), then route push delivery:
        deliver now, queue for quiet hours, or skip. The two are independent — a category can
        have push off and center on (or vice versa), so the center write never depends on
        anything push-related below it (#671, ADR-0102 §4)."""
        prefs = await self.prefs.get(tenant)
        effective = prefs.effective(category, automation_id)
        if effective.center:
            await self._notifications.create(
                tenant=tenant,
                category=category,
                title=title,
                body=body,
                deep_link=deep_link,
                entity_ref=entity_ref,
                automation_id=automation_id,
            )
        if not effective.push:
            return NotifyResult(outcome="skipped_disabled")
        local_now = await self._local_now()
        if is_quiet_now(prefs, local_now.time()):
            await self._queue.enqueue(
                tenant=tenant,
                category=category,
                title=title,
                body=body,
                deep_link=deep_link,
                entity_ref=entity_ref,
            )
            return NotifyResult(outcome="queued")
        if not self._check_rate_cap(tenant):
            return NotifyResult(outcome="skipped_rate_limited")
        return await self._send_now(tenant, category, title, body, deep_link, entity_ref)

    async def send_digest(self, tenant: str, items: list[QueuedPush]) -> NotifyResult:
        """Deliver one summary push for a batch of quiet-hours-held items (called by the scheduler).

        Bypasses ``notify``'s prefs/quiet-hours checks (already queued means they already
        passed) but still honors the rate cap — a burst of digests is exactly what it guards.
        """
        if not self._check_rate_cap(tenant):
            return NotifyResult(outcome="skipped_rate_limited")
        count = len(items)
        title = f"{count} notification{'s' if count != 1 else ''} while you were quiet"
        categories = sorted({item.category for item in items})
        body = ", ".join(categories)
        return await self._send_now(tenant, "digest", title, body, "/notifications", None)

    async def _send_now(
        self,
        tenant: str,
        category: str,
        title: str,
        body: str,
        deep_link: str | None,
        entity_ref: dict[str, Any] | None,
    ) -> NotifyResult:
        subs = await self._subscriptions.list(tenant)
        if not subs:
            return NotifyResult(outcome="skipped_no_devices")
        private_key_pem, _ = await self._vapid_keypair(tenant)
        payload = json.dumps(
            {
                "title": title,
                "body": body,
                "category": category,
                "deep_link": deep_link,
                "entity_ref": entity_ref,
            }
        )
        sent = 0
        pruned = 0
        for sub in subs:
            try:
                signer = load_vapid_signer(private_key_pem)
                await asyncio.to_thread(
                    webpush,
                    subscription_info={
                        "endpoint": sub.endpoint,
                        "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                    },
                    data=payload,
                    vapid_private_key=signer,
                    vapid_claims={"sub": self._vapid_subject},
                    ttl=_TTL_SECONDS,
                )
                sent += 1
            except WebPushException as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status in (404, 410):
                    # The push service no longer recognizes this registration (uninstalled
                    # PWA, cleared site data, expired) — expected churn, prune and move on.
                    await self._subscriptions.delete_by_endpoint(
                        tenant=tenant, endpoint=sub.endpoint
                    )
                    pruned += 1
                else:
                    log.warning("push send failed", tenant=tenant, status=status, error=str(exc))
        await self._emit_usage(tenant, category, sent)
        return NotifyResult(outcome="sent", sent_count=sent, pruned_count=pruned)

    async def _vapid_keypair(self, tenant: str) -> tuple[str, str]:
        """Read the tenant's stored VAPID keypair, generating and persisting one on first use."""
        try:
            data = await self._secrets.get(_VAPID_SECRET_PATH, tenant)
            return data["private_key"], data["public_key"]
        except SecretError:
            private_key, public_key = generate_vapid_keypair()
            await self._secrets.set(
                _VAPID_SECRET_PATH,
                {"private_key": private_key, "public_key": public_key},
                tenant,
            )
            log.info("generated a new VAPID keypair", tenant=tenant)
            return private_key, public_key

    def _check_rate_cap(self, tenant: str) -> bool:
        """A blunt per-tenant-per-hour cap across every category and device. 0 = unlimited."""
        if self._rate_cap_per_hour <= 0:
            return True
        now = _time.monotonic()
        window_start, count = self._rate_windows.get(tenant, (now, 0))
        if now - window_start >= 3600:
            window_start, count = now, 0
        if count >= self._rate_cap_per_hour:
            self._rate_windows[tenant] = (window_start, count)
            return False
        self._rate_windows[tenant] = (window_start, count + 1)
        return True

    async def _local_now(self) -> datetime:
        tz: tzinfo
        try:
            tz = ZoneInfo((await self._timezone()).strip() or "UTC")
        except Exception:  # unknown/blank/bad tz — fall back to UTC rather than skip the check
            tz = UTC
        return datetime.now(tz)

    async def _emit_usage(self, tenant: str, category: str, sent_count: int) -> None:
        """Publish a best-effort NATS usage event. Never breaks the send (mirrors llm.usage)."""
        try:
            await self._bus.publish(
                "push.sent",
                {"tenant": tenant, "category": category, "device_count": sent_count},
                tenant_id=tenant,
            )
        except Exception:
            log.warning("push usage event publish failed", exc_info=True)
