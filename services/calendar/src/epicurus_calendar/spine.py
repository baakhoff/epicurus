"""The shape and identity of calendar's spine emissions — shared by both emitters (#831).

Calendar announces the same three world-changes from two different places:

* the **provider-write seam** (:mod:`epicurus_calendar.providers.router`) — an operator or the
  agent creating, editing or deleting an event *through* this module, announced the moment the
  write lands (#664, ADR-0103);
* the **reconcile loop** (:mod:`epicurus_calendar.sync`) — a change made *outside* epicurus, in
  Google Calendar's own UI, noticed on the next incremental sync.

Both must produce identical payloads and ``dedup_key``s for the same change, and — more
sharply — the reconcile has to recognise a change the write seam already announced, so that one
operator action never becomes two events. Everything deciding either lives here, in one module
both sides import, because two copies of a dedup rule are one copy of a bug.

The **suppression key** is deliberately *not* the ``dedup_key``. A dedup key identifies a
change's exact resulting content (an update carries a change hash, so a genuinely different
edit is its own log entry); a suppression key identifies "this module wrote this event", which
must still match when the provider hands the content back slightly normalised, and when a
series-wide write comes back as one changed occurrence per instance. So it is
``"<event type>|<provider>:<id>"`` and nothing more — see
:meth:`~epicurus_calendar.sync_store.SelfWriteLedger.consume`.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from epicurus_calendar.models import Event

EVENT_CREATED = "calendar.event_created"
EVENT_UPDATED = "calendar.event_updated"
EVENT_CANCELLED = "calendar.event_cancelled"


def event_summary_payload(event: Event) -> dict[str, Any]:
    """Pointers + minimal metadata for a calendar event — never attendee emails or the
    description body (#664, mirrors mail's payload discipline)."""
    return {
        "title": event.title[:200],
        "start": event.start.isoformat(),
        "end": event.end.isoformat(),
        "all_day": event.all_day,
    }


def event_change_hash(event: Event) -> str:
    """A short, stable fingerprint of an event's mutable fields (#664's "dedup provider id +
    change hash"). Deliberately not Python's ``hash()`` — it is salted per-process
    (``PYTHONHASHSEED``), so the same content would hash differently across restarts, breaking
    the log's dedup guarantee for an update that straddles one.

    Since #831 it has a second job: the reconcile loop stores it alongside each observed event
    and compares the stored value with a freshly computed one to tell "this event changed" from
    "the provider mentioned this event again". Both sides therefore hash the *same* domain
    fields via the *same* function — a divergence would turn every reconcile pass into a storm
    of phantom ``event_updated``s.
    """
    fingerprint = {
        "title": event.title,
        "start": event.start.isoformat(),
        "end": event.end.isoformat(),
        "description": event.description,
        "location": event.location,
        "all_day": event.all_day,
    }
    digest = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()
    return digest[:12]


def created_dedup_key(provider: str, event_id: str) -> str:
    """``calendar.event_created``'s dedup key — the provider-qualified event id."""
    return f"{provider}:{event_id}"


def updated_dedup_key(provider: str, event_id: str, change_hash: str) -> str:
    """``calendar.event_updated``'s dedup key — provider id **plus** a change hash, so the same
    update re-observed twice dedups while a genuinely different edit is its own entry."""
    return f"{provider}:{event_id}:{change_hash}"


def cancelled_dedup_key(provider: str, event_id: str) -> str:
    """``calendar.event_cancelled``'s dedup key — the provider-qualified event id."""
    return f"{provider}:{event_id}"


def self_write_key(event_type: str, provider: str, event_id: str) -> str:
    """The self-write ledger key for "*this module* just made *this* change" (#831).

    Hash-free on purpose (see the module docstring): the reconcile must recognise its own
    write even when the provider normalises the content on the way back, and a series-wide
    write is announced once for the series but comes back as one change per occurrence — so the
    write seam records the key for the acted-on id *and* for its series, and the reconcile
    checks both.
    """
    return f"{event_type}|{provider}:{event_id}"
