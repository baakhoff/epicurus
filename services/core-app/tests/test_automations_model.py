"""Tests for the automations vocabulary — the autonomy dial, matchers, validation.

The dial is the centerpiece, and its promise is specific: a level's allowance is *derived*
and *enforced*, not requested. :func:`allowed_side_effects` is where the derivation lives,
so every level's allowance is pinned here — and the enforcement itself is proven in
test_automations_dial.py, against the real tool surface.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Any

import pytest

from epicurus_core_app.automations.model import (
    AUTONOMY_LEVELS,
    EventTrigger,
    PayloadMatcher,
    ScheduleTrigger,
    allowed_side_effects,
    matches_event,
    sinks_fire_for,
    validate_automation,
)

# ── the autonomy dial ────────────────────────────────────────────────────────


def test_notify_is_read_only() -> None:
    assert allowed_side_effects("notify") == frozenset({"read"})


def test_propose_adds_only_staging_tools() -> None:
    # "propose" is the class that stages for approval by construction (mail_send drafts,
    # knowledge_propose_* files a suggestion). Adding it must not add direct writes.
    assert allowed_side_effects("propose") == frozenset({"read", "propose"})


def test_act_adds_direct_writes() -> None:
    assert allowed_side_effects("act") == frozenset({"read", "propose", "write"})


def test_silent_act_reaches_exactly_as_far_as_act() -> None:
    # The two differ in audibility, not capability. A level that could act *more* while
    # saying less would be two dials wearing one name.
    assert allowed_side_effects("silent_act") == allowed_side_effects("act")


def test_the_levels_are_strictly_widening() -> None:
    # The dial's shape: each level is a superset of the one before. If this ever fails,
    # some level lets a tool through that a *higher* level forbids — an incoherent dial.
    order: list[Any] = ["notify", "propose", "act", "silent_act"]
    for lower, higher in pairwise(order):
        assert allowed_side_effects(lower) <= allowed_side_effects(higher)


def test_an_unknown_level_degrades_to_read_only() -> None:
    # A safety boundary must fail closed: a corrupted or future-versioned row gets the
    # narrowest allowance, never "everything".
    assert allowed_side_effects("nonsense") == frozenset({"read"})  # type: ignore[arg-type]


def test_only_silent_act_suppresses_sinks() -> None:
    assert [level for level in AUTONOMY_LEVELS if not sinks_fire_for(level)] == ["silent_act"]


# ── payload matchers ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("op", "value", "payload", "expected"),
    [
        ("eq", "a", {"f": "a"}, True),
        ("eq", "a", {"f": "b"}, False),
        ("ne", "a", {"f": "b"}, True),
        ("ne", "a", {"f": "a"}, False),
        ("contains", "lunch", {"f": "Re: lunch"}, True),
        ("contains", "lunch", {"f": "Re: dinner"}, False),
        ("gt", 5, {"f": 6}, True),
        ("gt", 5, {"f": 5}, False),
        ("lt", 5, {"f": 4}, True),
        ("lt", 5, {"f": 5}, False),
    ],
)
def test_matcher_ops(op: str, value: object, payload: dict[str, object], expected: bool) -> None:
    assert PayloadMatcher(field="f", op=op, value=value).test(payload) is expected  # type: ignore[arg-type]


def test_a_condition_on_an_absent_field_is_unmet() -> None:
    # Never vacuously true. An automation that fired *because* a field was missing would
    # be indefensible to explain.
    assert PayloadMatcher(field="missing", op="eq", value="a").test({"f": "a"}) is False
    assert PayloadMatcher(field="missing", op="ne", value="a").test({"f": "a"}) is False


def test_exists_matches_presence() -> None:
    assert PayloadMatcher(field="f", op="exists").test({"f": None}) is True
    assert PayloadMatcher(field="f", op="exists").test({}) is False


def test_a_non_numeric_value_is_a_non_match_not_a_crash() -> None:
    # One badly-typed payload must not take the matcher down for every other automation.
    assert PayloadMatcher(field="f", op="gt", value=5).test({"f": "abc"}) is False


# ── trigger windows ──────────────────────────────────────────────────────────


def test_no_window_is_always_live() -> None:
    assert EventTrigger(module="m", event_type="m.t").in_window(3) is True


def test_a_daytime_window_bounds_the_hours() -> None:
    trigger = EventTrigger(module="m", event_type="m.t", window_start_hour=9, window_end_hour=17)
    assert trigger.in_window(8) is False
    assert trigger.in_window(9) is True
    assert trigger.in_window(16) is True
    assert trigger.in_window(17) is False  # end is exclusive


def test_a_window_wrapping_midnight_is_a_union_not_an_empty_set() -> None:
    # 22:00→06:00. Read naively as start <= h < end this is empty, and an overnight
    # automation would silently never fire.
    trigger = EventTrigger(module="m", event_type="m.t", window_start_hour=22, window_end_hour=6)
    assert trigger.in_window(23) is True
    assert trigger.in_window(2) is True
    assert trigger.in_window(12) is False


def test_a_zero_width_window_is_exactly_that_hour() -> None:
    trigger = EventTrigger(module="m", event_type="m.t", window_start_hour=9, window_end_hour=9)
    assert trigger.in_window(9) is True
    assert trigger.in_window(10) is False


# ── matching ─────────────────────────────────────────────────────────────────


def _trigger(**kwargs: object) -> EventTrigger:
    base: dict[str, object] = {"module": "mail", "event_type": "mail.received"}
    base.update(kwargs)
    return EventTrigger(**base)  # type: ignore[arg-type]


def test_matches_on_module_and_type() -> None:
    assert matches_event(
        _trigger(), module="mail", event_type="mail.received", payload={}, local_hour=12
    )


def test_does_not_match_another_module_or_type() -> None:
    assert not matches_event(
        _trigger(), module="echo", event_type="mail.received", payload={}, local_hour=12
    )
    assert not matches_event(
        _trigger(), module="mail", event_type="mail.sent", payload={}, local_hour=12
    )


def test_every_matcher_must_pass() -> None:
    # AND, not OR: two conditions that should fire independently are two automations,
    # which keeps "why did this run?" answerable.
    trigger = _trigger(
        matchers=[
            PayloadMatcher(field="unread", op="eq", value=1),
            PayloadMatcher(field="subject", op="contains", value="lunch"),
        ]
    )
    assert matches_event(
        trigger,
        module="mail",
        event_type="mail.received",
        payload={"unread": 1, "subject": "Re: lunch"},
        local_hour=12,
    )
    assert not matches_event(
        trigger,
        module="mail",
        event_type="mail.received",
        payload={"unread": 1, "subject": "Re: dinner"},
        local_hour=12,
    )


def test_the_window_gates_a_match() -> None:
    trigger = _trigger(window_start_hour=9, window_end_hour=17)
    assert matches_event(
        trigger, module="mail", event_type="mail.received", payload={}, local_hour=10
    )
    assert not matches_event(
        trigger, module="mail", event_type="mail.received", payload={}, local_hour=3
    )


# ── validation ───────────────────────────────────────────────────────────────


def _validate(**overrides: object) -> None:
    kwargs: dict[str, object] = {
        "name": "A thing",
        "source": "user",
        "autonomy": "notify",
        "sinks": ["chat"],
        "event_trigger": _trigger(),
        "schedule_trigger": None,
        "rate_cap_per_hour": 0,
        "digest_window_minutes": 0,
    }
    kwargs.update(overrides)
    validate_automation(**kwargs)  # type: ignore[arg-type]


def test_a_valid_automation_passes() -> None:
    _validate()


def test_rejects_a_blank_name() -> None:
    with pytest.raises(ValueError, match="name must not be blank"):
        _validate(name="   ")


@pytest.mark.parametrize("source", ["user", "agent", "template:mail", "template:web-search"])
def test_accepts_every_legitimate_source(source: str) -> None:
    _validate(source=source)


@pytest.mark.parametrize("source", ["", "root", "template:", "template:Bad", "template:a.b"])
def test_rejects_a_malformed_source(source: str) -> None:
    with pytest.raises(ValueError, match="invalid source"):
        _validate(source=source)


def test_rejects_an_unknown_autonomy_level() -> None:
    with pytest.raises(ValueError, match="autonomy must be one of"):
        _validate(autonomy="yolo")


def test_rejects_an_unknown_sink() -> None:
    with pytest.raises(ValueError, match="unknown sink"):
        _validate(sinks=["chat", "carrier_pigeon"])


def test_requires_exactly_one_trigger() -> None:
    # None would never fire — a row that silently does nothing. Both would make "why did
    # this run?" ambiguous in the ledger.
    with pytest.raises(ValueError, match="exactly one trigger"):
        _validate(event_trigger=None, schedule_trigger=None)
    with pytest.raises(ValueError, match="exactly one trigger"):
        _validate(schedule_trigger=ScheduleTrigger(cadence="daily", hour=7))


def test_a_schedule_trigger_alone_is_valid() -> None:
    _validate(event_trigger=None, schedule_trigger=ScheduleTrigger(cadence="daily", hour=7))


def test_rejects_a_weekly_schedule_without_a_weekday() -> None:
    with pytest.raises(ValueError, match="weekday"):
        _validate(event_trigger=None, schedule_trigger=ScheduleTrigger(cadence="weekly", hour=7))


def test_rejects_an_out_of_range_schedule_hour() -> None:
    with pytest.raises(ValueError, match="hour must be 0-23"):
        _validate(event_trigger=None, schedule_trigger=ScheduleTrigger(cadence="daily", hour=24))


def test_rejects_an_unknown_cadence() -> None:
    with pytest.raises(ValueError, match="cadence must be one of"):
        _validate(
            event_trigger=None,
            schedule_trigger=ScheduleTrigger(cadence="hourly", hour=7),  # type: ignore[arg-type]
        )


def test_rejects_a_half_specified_window() -> None:
    with pytest.raises(ValueError, match="both a start and an end"):
        _validate(event_trigger=_trigger(window_start_hour=9))


def test_rejects_an_out_of_range_window_hour() -> None:
    with pytest.raises(ValueError, match="window hours must be 0-23"):
        _validate(event_trigger=_trigger(window_start_hour=9, window_end_hour=99))


def test_rejects_negative_caps() -> None:
    with pytest.raises(ValueError, match="rate_cap_per_hour"):
        _validate(rate_cap_per_hour=-1)
    with pytest.raises(ValueError, match="digest_window_minutes"):
        _validate(digest_window_minutes=-1)


def test_the_schedule_vocabulary_is_the_scheduled_turns_one() -> None:
    # The fold-in depends on this: a scheduled turn's (cadence, hour, weekday) migrates
    # across unchanged. If the vocabularies drift, the migration is a translation.
    from epicurus_core_app.scheduled_turns import validate_cadence

    trigger = ScheduleTrigger(cadence="weekly", hour=7, weekday=2)
    validate_cadence(trigger.cadence, trigger.weekday)  # the old validator accepts it
    _validate(event_trigger=None, schedule_trigger=trigger)
