"""Web push (#670, ADR-0102): VAPID-signed browser push, quiet hours, category prefs.

Public surface re-exported here; see each submodule's docstring for its slice of the
send path (:mod:`vapid`, :mod:`subscriptions`, :mod:`prefs`, :mod:`queue`, :mod:`service`,
:mod:`routes`).
"""

from __future__ import annotations

from epicurus_core_app.push.prefs import ChannelPrefs, PushPrefs, PushPrefsStore
from epicurus_core_app.push.queue import PushDigestScheduler, PushQueueStore, QueuedPush
from epicurus_core_app.push.routes import create_push_router
from epicurus_core_app.push.service import NotifyResult, PushService
from epicurus_core_app.push.subscriptions import PushSubscription, PushSubscriptionStore

__all__ = [
    "ChannelPrefs",
    "NotifyResult",
    "PushDigestScheduler",
    "PushPrefs",
    "PushPrefsStore",
    "PushQueueStore",
    "PushService",
    "PushSubscription",
    "PushSubscriptionStore",
    "QueuedPush",
    "create_push_router",
]
