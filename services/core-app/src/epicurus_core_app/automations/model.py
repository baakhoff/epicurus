"""The automations vocabulary — autonomy levels, triggers, sinks, and the row shape.

Pure values and pure functions: no database, no bus, no agent. The runner, the store, and
the routes all agree on the meanings defined here, and they can be tested without any of
those things being present.

## The autonomy dial (ADR-0105)

Four levels, each strictly wider than the last:

1. ``notify`` — look, don't touch. Read-only tools; the answer goes to the sinks.
2. ``propose`` — may draft. Adds tools that **stage for approval by construction**
   (``mail_send`` composes a draft; ``knowledge_propose_*`` files a suggestion).
3. ``act`` — may change things. Adds direct writes; the run report goes to the sinks.
4. ``silent_act`` — the same reach as ``act``, but reports **only** to the run ledger. For
   the boring chores you want done and never mentioned (mark the newsletters read).

The dial's promise is that a level's allowance is **derived from the tool's declared side
effect and enforced at the turn's tool surface** — a ``notify`` automation is not asked
nicely to avoid writing, it is handed no tool that can. :func:`allowed_side_effects` is
that derivation, and it is the only place the mapping exists.

Note what separates 3 from 4: not capability, but *audibility*. That is deliberate — a
level that could act more but say less would be two dials wearing one name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from epicurus_core import SideEffect

__all__ = [
    "AUTONOMY_LEVELS",
    "CADENCES",
    "SINKS",
    "Automation",
    "AutomationRun",
    "AutonomyLevel",
    "Cadence",
    "EventTrigger",
    "PayloadMatcher",
    "ScheduleTrigger",
    "Sink",
    "Source",
    "allowed_side_effects",
    "matches_event",
    "sinks_fire_for",
    "validate_automation",
]

AutonomyLevel = Literal["notify", "propose", "act", "silent_act"]
AUTONOMY_LEVELS: tuple[AutonomyLevel, ...] = ("notify", "propose", "act", "silent_act")

Sink = Literal["push", "chat", "notes", "kb"]
SINKS: tuple[Sink, ...] = ("push", "chat", "notes", "kb")

Source = Literal["user", "agent"]  # plus "template:<module>", checked by _valid_source

Cadence = Literal["daily", "weekly"]
CADENCES: tuple[Cadence, ...] = ("daily", "weekly")

ChatMode = Literal["rolling", "per_run"]

# "user" | "agent" | "template:<module>" — the module token matches the manifest's own
# name rules (see epicurus_core.module_events), so a source is always a safe identifier.
_SOURCE_RE = re.compile(r"^(user|agent|template:[a-z0-9][a-z0-9_-]*)$")

# Which side effects each level may reach. The single source of truth for the dial.
_ALLOWANCES: dict[AutonomyLevel, frozenset[SideEffect]] = {
    "notify": frozenset({"read"}),
    "propose": frozenset({"read", "propose"}),
    "act": frozenset({"read", "propose", "write"}),
    # Same reach as `act` — the difference is that no sink fires (see sinks_fire_for).
    "silent_act": frozenset({"read", "propose", "write"}),
}


def allowed_side_effects(level: AutonomyLevel) -> frozenset[SideEffect]:
    """The tool classes *level* may use — handed straight to ``McpHost.discover(allow=…)``.

    An unknown level yields the narrowest allowance rather than raising: this is a safety
    boundary, and a corrupted or future-versioned row should degrade to read-only, never to
    "everything". (:func:`validate_automation` is what rejects a bad level on the way in;
    this is the belt to that pair of braces.)
    """
    return _ALLOWANCES.get(level, frozenset({"read"}))


def sinks_fire_for(level: AutonomyLevel) -> bool:
    """Whether a run at *level* delivers to its sinks at all.

    Only ``silent_act`` says no: it acts and reports to the ledger alone. The sinks stay
    configured on the row — flipping the level back to ``act`` restores them, rather than
    making the operator rebuild what they wanted announced.
    """
    return level != "silent_act"


@dataclass(frozen=True)
class PayloadMatcher:
    """One deterministic condition on a triggering event's payload.

    Deterministic on purpose — a filter must not need the model. Matching runs before the
    agent step and decides whether a turn happens at all, so putting an LLM here would mean
    paying for inference to decide whether to pay for inference, and would make "why did
    this fire?" unanswerable.

    ``field`` is a top-level payload key (the payload is a flat pointer bag by contract).
    """

    field: str
    op: Literal["eq", "ne", "contains", "exists", "gt", "lt"]
    value: Any = None

    def test(self, payload: dict[str, Any]) -> bool:
        """Whether *payload* satisfies this condition."""
        present = self.field in payload
        if self.op == "exists":
            return present is bool(self.value) if self.value is not None else present
        if not present:
            # A condition about an absent field is unmet — never vacuously true. An
            # automation that fired because a field was missing would be indefensible.
            return False
        actual = payload[self.field]
        if self.op == "eq":
            return bool(actual == self.value)
        if self.op == "ne":
            return bool(actual != self.value)
        if self.op == "contains":
            return isinstance(actual, str) and str(self.value) in actual
        # gt / lt are numeric-only; a non-comparable value is a non-match, not a crash —
        # one badly-typed payload must not take the matcher down for every automation.
        try:
            if self.op == "gt":
                return bool(float(actual) > float(self.value))
            return bool(float(actual) < float(self.value))
        except (TypeError, ValueError):
            return False


@dataclass(frozen=True)
class EventTrigger:
    """Fire when a module event arrives and every matcher passes.

    ``window_start_hour``/``window_end_hour`` optionally bound the *local* hours in which
    the trigger is live (e.g. only during the working day). Both unset = always live.
    """

    module: str
    event_type: str
    matchers: list[PayloadMatcher] = field(default_factory=list)
    window_start_hour: int | None = None
    window_end_hour: int | None = None

    def in_window(self, local_hour: int) -> bool:
        """Whether *local_hour* is inside the trigger's active window.

        A window that wraps midnight (22→6) is expressed by start > end and read as the
        union of the two ends of the day, not as an empty set.
        """
        start, end = self.window_start_hour, self.window_end_hour
        if start is None or end is None:
            return True
        if start == end:
            return local_hour == start
        if start < end:
            return start <= local_hour < end
        return local_hour >= start or local_hour < end


@dataclass(frozen=True)
class ScheduleTrigger:
    """Fire on a cadence at a local hour — the #621/ADR-0092 schedule vocabulary, reused.

    Deliberately the same fields ``scheduled_turns`` already had (``cadence``, ``hour``,
    ``weekday``), because a scheduled turn *is* one of these now: the fold-in migrates rows
    into automations rather than translating between two schedule dialects forever.
    """

    cadence: Cadence
    hour: int
    weekday: int | None = None  # 0=Monday..6=Sunday; required when cadence == "weekly"


@dataclass
class Automation:
    """One automation (tenant-scoped) — the value the store returns."""

    id: str
    tenant: str
    name: str
    enabled: bool
    source: str  # "user" | "agent" | "template:<module>"
    event_trigger: EventTrigger | None
    schedule_trigger: ScheduleTrigger | None
    prompt: str
    model: str | None  # None → the tenant's default chat model
    autonomy: AutonomyLevel
    sinks: list[Sink]
    chat_mode: ChatMode
    chat_session_id: str | None  # the rolling session a chat sink delivers into
    rate_cap_per_hour: int  # 0 = uncapped
    digest_window_minutes: int  # 0 = run per event, no batching
    created_at: datetime
    last_run_at: datetime | None = None
    last_status: str | None = None

    def allowed(self) -> frozenset[SideEffect]:
        """The tool classes this automation's turn may reach."""
        return allowed_side_effects(self.autonomy)

    def fires_sinks(self) -> bool:
        """Whether a run of this automation delivers to its sinks."""
        return sinks_fire_for(self.autonomy)


@dataclass
class AutomationRun:
    """One entry in the run ledger — what happened, and what it cost."""

    id: str
    tenant: str
    automation_id: str
    started_at: datetime
    trigger_refs: list[int]  # module_events row ids that caused it (empty for a schedule)
    filter_verdict: str  # "matched" | "digest" | "schedule" | why it was skipped
    model: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    duration_ms: int | None
    outcome: str  # "ok" | "error" | "skipped"
    error: str | None
    output: str  # the turn's answer — recorded even when no sink fires
    sinks_fired: list[str]


def validate_automation(
    *,
    name: str,
    source: str,
    autonomy: str,
    sinks: list[str],
    event_trigger: EventTrigger | None,
    schedule_trigger: ScheduleTrigger | None,
    rate_cap_per_hour: int,
    digest_window_minutes: int,
) -> None:
    """Raise ``ValueError`` if the parts don't form a coherent automation.

    Called on the way in — from the routes and from template instantiation — so a row that
    reaches the runner is already known-good. The runner must never be the thing that
    discovers a broken automation, because by then it is mid-turn.
    """
    if not name.strip():
        raise ValueError("name must not be blank")
    if not _SOURCE_RE.fullmatch(source):
        raise ValueError(
            f"invalid source {source!r}: expected 'user', 'agent', or 'template:<module>'"
        )
    if autonomy not in AUTONOMY_LEVELS:
        raise ValueError(f"autonomy must be one of {list(AUTONOMY_LEVELS)}, got {autonomy!r}")
    unknown = [s for s in sinks if s not in SINKS]
    if unknown:
        raise ValueError(f"unknown sink(s) {unknown}; expected any of {list(SINKS)}")
    # Exactly one trigger: none would never fire (a row that silently does nothing), and
    # both would make "why did this run?" ambiguous in the ledger.
    if (event_trigger is None) == (schedule_trigger is None):
        raise ValueError("an automation needs exactly one trigger: event or schedule")
    if schedule_trigger is not None:
        if schedule_trigger.cadence not in CADENCES:
            raise ValueError(
                f"cadence must be one of {list(CADENCES)}, got {schedule_trigger.cadence!r}"
            )
        if not (0 <= schedule_trigger.hour <= 23):
            raise ValueError("hour must be 0-23")
        if schedule_trigger.cadence == "weekly" and (
            schedule_trigger.weekday is None or not (0 <= schedule_trigger.weekday <= 6)
        ):
            raise ValueError("weekday (0=Monday..6=Sunday) is required for a weekly cadence")
    if event_trigger is not None:
        for hour in (event_trigger.window_start_hour, event_trigger.window_end_hour):
            if hour is not None and not (0 <= hour <= 23):
                raise ValueError("trigger window hours must be 0-23")
        if (event_trigger.window_start_hour is None) != (event_trigger.window_end_hour is None):
            raise ValueError("a trigger window needs both a start and an end hour")
    if rate_cap_per_hour < 0:
        raise ValueError("rate_cap_per_hour must be >= 0 (0 = uncapped)")
    if digest_window_minutes < 0:
        raise ValueError("digest_window_minutes must be >= 0 (0 = no batching)")


def matches_event(
    trigger: EventTrigger,
    *,
    module: str,
    event_type: str,
    payload: dict[str, Any],
    local_hour: int,
) -> bool:
    """Whether an event satisfies *trigger* — module, type, every matcher, and the window.

    All matchers must pass (AND). There is no OR: two conditions that should fire
    independently are two automations, which keeps the ledger's "why did this run?"
    answerable and the filter readable without a query language.
    """
    return (
        trigger.module == module
        and trigger.event_type == event_type
        and trigger.in_window(local_hour)
        and all(m.test(payload) for m in trigger.matchers)
    )
