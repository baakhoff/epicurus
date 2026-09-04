"""Background mailbox reconcile — ``mail.received`` without the Mail page open (#796).

Until this existed, ``mail.received`` was emitted from exactly one place
(:meth:`~epicurus_mail.cache.CachedMailbox.reconcile`) reached from exactly one caller: the
mailbox page's ``?reconcile=1`` read, which only fires when a human opens Mail. So an
automation on "new mail arrived", and the per-event push alert behind it, could only ever run
*after* the operator had already seen the mail — the exact inverse of what "notify me when mail
arrives" means. Nothing downstream was broken; they were all waiting on an event nobody emitted.

This is the missing caller, and deliberately *only* a caller: a tick is
``availability()`` → ``reconcile(label)``, the same method the page calls, so every consumer
(the event intake, the automation matcher, the alert listener) works unmodified and the two
paths can never drift. Calendar and tasks already ship exactly this shape
(``scheduler.run_periodic``); mail was the outlier.

Three properties the loop owes its neighbours:

- **An idle deployment costs nothing and says nothing.** With no account connected, a tick is
  one token-presence check (no provider API call, #209) and a return — no reconcile, no log
  line. A poller that narrated its own inactivity every few minutes would be worse than no
  poller at all.
- **A blocked deployment says so, once.** "Nobody connected Google" and "we couldn't ask" are
  different facts (#835) and the loop treats them differently: silence for the first, a single
  warning naming the reason plus a widening back-off for the second. Idling quietly through an
  unreachable core is how a mailbox stops syncing for hours with nothing in the log to say why.
- **It never double-fires.** ``reconcile`` is single-flight per account (#796), so a poll tick
  and a page-triggered reconcile serialize on the change cursor rather than both announcing the
  same message.

Polling, not push: Gmail's ``users.watch`` + Pub/Sub would be lower-latency, but it needs a
publicly reachable endpoint and a Google Cloud project — neither of which a local-first
self-host can assume — and it is Gmail-only, where this loop carries over to IMAP unchanged.
"""

from __future__ import annotations

import asyncio

from epicurus_core import get_logger
from epicurus_mail.cache import CachedMailbox
from epicurus_mail.provider import MailAvailability, MailProvider

log = get_logger("epicurus_mail.poller")

DEFAULT_POLL_INTERVAL_S = 300.0
"""How often the background reconcile ticks.

A tick is one history-delta call (plus a summary fetch per changed thread), so it is cheap
against any provider quota — but "cheap" is not "free", and mail is not minute-critical the way
calendar's lead times are (contrast its 60s). Five minutes bounds worst-case notification
latency at five minutes while leaving a mailbox that receives nothing costing ~288 delta calls a
day, well inside Gmail's per-user quota.
"""

DEFAULT_POLL_LABEL = "INBOX"
"""Which folder the background tick reconciles.

The delta itself is mailbox-wide (a provider change cursor is not folder-scoped), so this only
picks which folder's *cached rows* the tick keeps warm — and the Inbox is both the landing view
and the folder backing the nav badge. ``mail.received`` still reports each message's own folder,
derived from the message rather than from this label.
"""


MAX_UNREACHABLE_BACKOFF = 4.0
"""How far the interval stretches while the provider stays unreachable (#835).

Multiplied by two per consecutive unreachable tick and capped here, so a core that is down for
an hour is probed on a widening schedule (5, 10, 20, 20 … minutes at the default interval)
instead of every five minutes forever. Capped rather than unbounded because the loop is also
the recovery path: back off far enough and a mailbox stays stale long after the core returned.
Reset by any tick that gets an answer — connected *or* not_connected.
"""


async def tick(*, mailbox: CachedMailbox, provider: MailProvider, label: str) -> MailAvailability:
    """One poll pass. Returns the availability that decided what it did (#835).

    The availability check is the cheap credential probe (#209), never a live provider call, so
    the common self-host case — the module is deployed, nobody has connected Gmail — costs one
    core round-trip and stops there.

    It reconciles on ``connected`` only. The other two states both mean "no reconcile", but
    they are returned distinctly rather than collapsed to a bool because the caller logs and
    paces them differently: ``not_connected`` is a quiet, expected steady state, while
    ``unreachable`` is a fault that has to reach the operator. Returning the availability
    (rather than acting on it here) keeps the tick a single pass with no timing of its own,
    which is what makes it directly testable.
    """
    availability = await provider.availability()
    if availability.connected:
        await mailbox.reconcile(label)
    return availability


async def run_periodic(
    *,
    mailbox: CachedMailbox,
    provider: MailProvider,
    tenant: str,
    label: str = DEFAULT_POLL_LABEL,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
) -> None:
    """Reconcile *label* every *poll_interval_s* seconds, forever.

    Ticks first and sleeps after, like calendar's and tasks' schedulers: a restart should notice
    what arrived while the service was down straight away rather than after a full interval — and
    a resumed sync is precisely where the backlog replay lives
    (:meth:`~epicurus_mail.cache.CachedMailbox._full_sync`).

    A non-positive *poll_interval_s* disables the loop and returns immediately: an operator who
    turns the poller off gets no background task at all, not one that spins. One bad tick (an
    expired token, a provider hiccup) is logged and skipped, never kills the loop — and repeats
    are logged at debug, so an account that stays broken produces one warning plus a recovery
    line rather than a warning every interval. The underlying failure is separately announced on
    the spine as ``mail.sync_failed``, itself rate-limited.

    An **unreachable** provider (#835) is handled as its own kind of bad tick rather than as an
    idle one. Before the three-state signal existed it was indistinguishable from "nobody
    connected Google", so the loop took the idle path: no reconcile, no log, no change of pace —
    a mailbox could stop syncing for as long as the core was down and leave nothing behind
    saying so. Now it warns once (naming the reason), repeats at debug, and stretches the
    interval up to :data:`MAX_UNREACHABLE_BACKOFF` so a downed core isn't probed at full rate,
    logging the recovery when an answer comes back. ``not_connected`` keeps the silent path it
    has always had — that one really is an operator's choice, not a fault.
    """
    if poll_interval_s <= 0:
        log.info("mail background reconcile disabled", tenant=tenant)
        return
    log.info(
        "mail background reconcile started",
        tenant=tenant,
        interval_s=poll_interval_s,
        label=label,
    )
    failing = False
    backoff = 1.0
    while True:
        delay = poll_interval_s
        try:
            availability = await tick(mailbox=mailbox, provider=provider, label=label)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            backoff = 1.0
            if failing:
                log.debug("mail background reconcile still failing", tenant=tenant, error=str(exc))
            else:
                log.warning("mail background reconcile failed", tenant=tenant, error=str(exc))
                failing = True
        else:
            if availability.state == "unreachable":
                if failing:
                    log.debug(
                        "mail provider still unreachable",
                        tenant=tenant,
                        reason=availability.reason,
                    )
                else:
                    log.warning(
                        "mail provider unreachable; background reconcile paused",
                        tenant=tenant,
                        reason=availability.reason,
                    )
                    failing = True
                delay = poll_interval_s * backoff
                backoff = min(backoff * 2, MAX_UNREACHABLE_BACKOFF)
            else:
                backoff = 1.0
                if failing:
                    log.info(
                        "mail background reconcile recovered",
                        tenant=tenant,
                        state=availability.state,
                    )
                    failing = False
        await asyncio.sleep(delay)
