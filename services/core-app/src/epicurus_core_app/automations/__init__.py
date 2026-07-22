"""Automations — the engine that turns a world change into an assistant action (ADR-0105).

The event spine (ADR-0103) records that something happened. This decides whether anything
should be done about it, does it at an autonomy level the operator chose, and writes down
what it did.

Layered so each piece is testable without the ones above it:

* :mod:`~epicurus_core_app.automations.model` — the vocabulary. Autonomy levels, triggers,
  matchers, sinks. Pure values; no database, no bus, no agent.
* :mod:`~epicurus_core_app.automations.store` — the tenant-scoped ``automations`` table,
  the ``automation_runs`` ledger, the durable ``automation_queue``, and the kill switch.
* :mod:`~epicurus_core_app.automations.runner` — the matcher on the event intake, the
  schedule tick, and the run: an agent turn with the triggering events in context, then a
  deterministic sink fan-out.
* :mod:`~epicurus_core_app.automations.sinks` — where a run's output goes, behind a seam
  that degrades gracefully while the real sinks are companion issues.
"""

from __future__ import annotations

from epicurus_core_app.automations.model import (
    AUTONOMY_LEVELS,
    SINKS,
    Automation,
    AutomationRun,
    AutonomyLevel,
    EventTrigger,
    PayloadMatcher,
    ScheduleTrigger,
    Sink,
    allowed_side_effects,
    matches_event,
    sinks_fire_for,
    validate_automation,
)
from epicurus_core_app.automations.runner import AutomationRunner, AutomationScheduler
from epicurus_core_app.automations.sinks import SinkDispatcher, SinkResult
from epicurus_core_app.automations.store import (
    AutomationQueue,
    AutomationStore,
    KillSwitchStore,
    QueuedTrigger,
)

__all__ = [
    "AUTONOMY_LEVELS",
    "SINKS",
    "Automation",
    "AutomationQueue",
    "AutomationRun",
    "AutomationRunner",
    "AutomationScheduler",
    "AutomationStore",
    "AutonomyLevel",
    "EventTrigger",
    "KillSwitchStore",
    "PayloadMatcher",
    "QueuedTrigger",
    "ScheduleTrigger",
    "Sink",
    "SinkDispatcher",
    "SinkResult",
    "allowed_side_effects",
    "matches_event",
    "sinks_fire_for",
    "validate_automation",
]
