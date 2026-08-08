"""Per-event alerts (#732) — a dumb fan-out from the module event spine to push/center.

:class:`EventAlertListener` is wired to :meth:`epicurus_core_app.event_log.EventIntake.
on_event`, the same seam :class:`~epicurus_core_app.automations.runner.AutomationMatcher`
uses — but it is deliberately **not** an automation: no agent turn, no trigger queue, no
ledger entry, no autonomy dial. An enabled subscription just becomes a notification. Want
reasoning or actions on the event instead of a tap on the shoulder? That is what an
automation's event trigger is for.

An automation with an event trigger on the same ``(module, event_type)`` as an enabled
alert both fire on the same occurrence — two notifications, by design. Nothing here
suppresses one for the other; deduping them would require the alert to know about every
automation's trigger, which is exactly the coupling a "dumb fan-out" exists to avoid.

The per-subscription rate cap below is independent of, and in addition to,
:class:`~epicurus_core_app.push.service.PushService`'s own tenant-wide push cap: a single
chatty event type must not be able to spend a tenant's *entire* hourly push budget on one
subscription, starving every other category and alert. It gates the whole notification
(push and center together), not push alone — a firehose into the notification center is
still a firehose even when no device is subscribed to receive a push for it.

An *enabled* subscription always lands in the notification center (#797 — the center is a
superset of push, see ``push/service.py``): the subscription's ``push`` flag decides whether
devices are also notified, and its stored ``center`` flag only matters as "either channel on
means the alert is on" in the gate below. Both decline paths log a distinct line — ``event
alert declined: no subscription for this event`` and ``event alert rate cap reached`` — so
silence is always attributable to a specific gate rather than a mystery.
"""

from __future__ import annotations

import time as _time
from typing import Any, Protocol

from epicurus_core import get_logger
from epicurus_core_app.event_log import LoggedEvent
from epicurus_core_app.push.event_subscriptions import EventSubscriptionStore
from epicurus_core_app.push.prefs import ChannelPrefs
from epicurus_core_app.push.service import NotifyResult

__all__ = ["EventAlertListener", "Notifier"]

log = get_logger("epicurus_core_app.push.event_alerts")


class Notifier(Protocol):
    """The slice of ``PushService`` the listener needs — so tests need no push stack.

    Mirrors ``PushService.notify_effective``'s signature deliberately (``automations.
    runner.TurnRunner`` documents the same reasoning for ``Agent``): if that method gains a
    parameter this must too, or the listener silently stops passing it.
    """

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
    ) -> NotifyResult: ...


class EventAlertListener:
    """Turns a recorded event into a notification, if — and only if — the tenant subscribed."""

    def __init__(
        self,
        subscriptions: EventSubscriptionStore,
        push: Notifier,
        *,
        rate_cap_per_hour: int,
    ) -> None:
        self._subscriptions = subscriptions
        self._push = push
        self._rate_cap_per_hour = rate_cap_per_hour
        # (tenant, module, event_type) -> (window_start_monotonic, count). In-memory,
        # single-instance v1 — the same disposable-cache trade PushService's own tenant-wide
        # cap makes (losing counts on a restart just under-limits for one window).
        self._rate_windows: dict[tuple[str, str, str], tuple[float, int]] = {}

    async def on_event(self, entry: LoggedEvent) -> None:
        """Notify on *entry* if its tenant has an enabled subscription for its event.

        Registered as an ``EventIntake`` listener, which already logs-and-continues on a
        raising listener — so, like ``AutomationMatcher.on_event``, this does not need its
        own top-level guard for that. Fires on every matching event regardless of
        ``causation_id``: unlike the automations matcher's loop guard, there is no agent turn
        here to spiral, so an event an automation's own run produced is exactly as
        alert-worthy as one a module emitted.
        """
        prefs = await self._subscriptions.get(
            entry.tenant, module=entry.module, event_type=entry.type
        )
        if prefs is None or not (prefs.push or prefs.center):
            # The most invisible gate on the whole notification path (#797): everything
            # defaults to unsubscribed, so "no notification" is usually *this*, working as
            # configured — logged so an operator can tell it from a broken pipeline. Info,
            # one line per recorded event, the same volume as intake's "event recorded".
            log.info(
                "event alert declined: no subscription for this event",
                tenant=entry.tenant,
                module=entry.module,
                type=entry.type,
            )
            return
        key = (entry.tenant, entry.module, entry.type)
        if not self._check_rate_cap(key):
            log.warning(
                "event alert rate cap reached",
                tenant=entry.tenant,
                module=entry.module,
                type=entry.type,
            )
            return
        title, body = _render(entry)
        await self._push.notify_effective(
            entry.tenant,
            effective=prefs,
            category=entry.module,
            title=title,
            body=body,
            entity_ref=entry.entity_ref.model_dump() if entry.entity_ref else None,
        )

    def _check_rate_cap(self, key: tuple[str, str, str]) -> bool:
        """A blunt per-subscription-per-hour cap. 0 = unlimited."""
        if self._rate_cap_per_hour <= 0:
            return True
        now = _time.monotonic()
        window_start, count = self._rate_windows.get(key, (now, 0))
        if now - window_start >= 3600:
            window_start, count = now, 0
        if count >= self._rate_cap_per_hour:
            self._rate_windows[key] = (window_start, count)
            return False
        self._rate_windows[key] = (window_start, count + 1)
        return True


def _render(entry: LoggedEvent) -> tuple[str, str]:
    """(title, body) for a compact, generic notification — no per-module knowledge.

    The entity's own title leads when the event carries one (an operator would rather see
    "Q3 invoice" than "mail.received"); the module/type pair always appears, in the title
    when there is nothing more specific, in the body otherwise. The client already renders
    ``entity_ref`` as a hover-card chip (ADR-0019), so this never duplicates its detail.
    """
    identity = f"{entry.module} · {entry.type}"
    if entry.entity_ref is not None and entry.entity_ref.title:
        return entry.entity_ref.title, identity
    return identity, ""
