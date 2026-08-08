"""The ``push`` sink — a run's output delivered as a browser push + a notification-center
row (#723, extending ADR-0102/ADR-0104/ADR-0108).

``push`` has been valid sink vocabulary since ADR-0105 (:data:`~epicurus_core_app.
automations.model.SINKS`), and every one of the ten starter templates #717 shipped declares
``sinks=["push"]`` — but until this module, nothing was ever registered for it. A run
configured with only ``push`` executed, recorded a ledger row, and delivered nothing an
operator could see. This closes that gap the same way :mod:`document_sinks` closed it for
``notes``/``kb``: a thin adapter between the sink seam (:mod:`~epicurus_core_app.
automations.sinks`) and the delivery path that already exists and is already gated.

The handler goes **through** :meth:`~epicurus_core_app.push.service.PushService.notify`,
never around it: quiet hours, the tenant-wide rate cap, and the per-category/per-automation
push/center toggles (``PushPrefs.effective``) all apply exactly as they do for any other
caller (the settings UI's test button, #732's event alerts). It never touches
``PushService``'s lower-level send path directly, and it never bypasses a preference an
operator set.

**Category.** Every automation's push/center notification is filed under the ``"automation"``
category (:data:`epicurus_core_app.push.prefs.KNOWN_CATEGORIES`) — a single settings-UI toggle
row governs every automation by default. ``automation_id`` is also passed on every call, so
``PushPrefs.effective`` can apply a **per-automation override**
(``PushPrefsStore.set_automation_override``) when one exists — the seam ADR-0102 §4 built
for exactly this, unused until now: an operator can silence one noisy automation without
muting the category for every other one.

**Title/body.** The title is the automation's own name — the label the operator chose,
recognizable the way ``push.event_alerts._render`` prefers an entity's own title over a
generic type string. The body is the run's raw output, untruncated: it is handed to
``notify()`` exactly as the turn produced it, so the notification-center row keeps the full
text (ADR-0104 — a durable record independent of whether push delivery itself fires, queues,
or is skipped). ``PushService._send_now`` is what shortens the outgoing *wire* payload for
push's own size limits; that trim never reaches the stored row.

**Deep link.** ``/observability?tab=runs&automation=<id>`` — the existing, already-wired
deep-link target the Automations page's own per-row run history uses (`docs/reference/
automations.md#the-automations-page-668`), landing the operator on this automation's run
history rather than the flat automations list.

**Artifact.** ``notify()`` returns the notification-center row's id — since #797
unconditionally, because the center records every notification that fires (the center is a
superset of push; a per-automation override can still silence *push*, never the row). The
handler returns an ``EntityRef`` pointing at it, the same way
:func:`document_sinks.make_document_sink` returns one for the document it wrote —
``module="core"``, no resolver, exactly the precedent
:meth:`~epicurus_core_app.core_events.CoreEventEmitter._file_ref` already sets for the Files
page: the runs feed still renders a chip from the ref's own title even with nothing to
hover-resolve. The no-id branch below survives only as defense against a notifier that
returns none; the real send path no longer has such a case.
"""

from __future__ import annotations

from typing import Any, Protocol

from epicurus_core import EntityRef, get_logger
from epicurus_core_app.automations.model import Automation
from epicurus_core_app.automations.sinks import SinkHandler
from epicurus_core_app.push.service import NotifyResult

__all__ = ["PUSH_CATEGORY", "Notifier", "make_push_sink"]

log = get_logger("epicurus_core_app.automations.push_sink")

#: The fixed, platform-owned category (`epicurus_core_app.push.prefs.KNOWN_CATEGORIES`) every
#: automation's push/center notification is filed under. An operator who wants to silence one
#: automation individually uses `PushPrefs.automation_overrides` instead — `automation_id` is
#: passed on every call below for exactly that (see the module docstring's Category section).
PUSH_CATEGORY = "automation"

#: A defensive cap on the notification title, mirroring `core_events.py`'s `name[:200]` — an
#: automation's name has no length limit at the model layer, and a push notification is not
#: the place to discover one the hard way.
_MAX_TITLE_CHARS = 200

#: What a run with no output (a turn that only called tools, or answered with whitespace)
#: shows instead of an empty push notification.
_EMPTY_OUTPUT_BODY = "The run completed with no output."


class Notifier(Protocol):
    """The slice of ``PushService`` the push sink needs — so tests need no push stack.

    Mirrors ``PushService.notify``'s signature deliberately (``push.event_alerts.Notifier``
    documents the same reasoning for ``notify_effective``): if that method gains a parameter
    this must too, or the sink silently stops passing it.
    """

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
    ) -> NotifyResult: ...


def _deep_link(automation: Automation) -> str:
    """This automation's filtered run history on the observability runs feed."""
    return f"/observability?tab=runs&automation={automation.id}"


def make_push_sink(push: Notifier) -> SinkHandler:
    """Build the ``push`` sink handler.

    Closed over the ``PushService`` (as :class:`Notifier`). Tenant-scoped by construction:
    every call routes through ``automation.tenant``, taken from the automation the dispatcher
    handed in — never a default or ambient tenant, so one tenant's run can never notify
    another's devices.
    """

    async def handler(automation: Automation, output: str) -> EntityRef | None:
        title = automation.name.strip()[:_MAX_TITLE_CHARS] or "Automation"
        body = output.strip() or _EMPTY_OUTPUT_BODY
        result = await push.notify(
            automation.tenant,
            category=PUSH_CATEGORY,
            title=title,
            body=body,
            deep_link=_deep_link(automation),
            automation_id=automation.id,
        )
        log.info(
            "automation push sink delivered",
            automation=automation.id,
            tenant=automation.tenant,
            outcome=result.outcome,
        )
        if result.notification_id is None:
            # The center toggle is off for this category/automation (or an override turned it
            # off) — nothing durable to point the runs feed at. See the module docstring's
            # Artifact section: this is the one case notes/kb never hits.
            return None
        return EntityRef(
            ref_id=result.notification_id,
            module="core",
            kind="notification",
            title=title,
        )

    return handler
