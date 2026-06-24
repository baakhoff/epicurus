"""Unit tests for the ordered activity timeline (think → tool → think) — #300."""

from __future__ import annotations

from epicurus_core_app.agent.activity import (
    MessageActivity,
    ThinkingItem,
    activity_from_timeline,
    append_thinking,
    append_tool,
)


def test_append_thinking_coalesces_consecutive_runs() -> None:
    timeline: list = []
    append_thinking(timeline, "let me ")
    append_thinking(timeline, "think")
    assert len(timeline) == 1
    assert isinstance(timeline[0], ThinkingItem)
    assert timeline[0].text == "let me think"


def test_a_tool_splits_thinking_into_two_blocks() -> None:
    timeline: list = []
    append_thinking(timeline, "plan")
    append_tool(timeline, "echo", "ok", None)
    append_thinking(timeline, "now answer")
    kinds = [i.model_dump()["kind"] for i in timeline]
    assert kinds == ["thinking", "tool", "thinking"]
    assert timeline[0].model_dump()["text"] == "plan"
    assert timeline[2].model_dump()["text"] == "now answer"


def test_activity_from_timeline_derives_flat_fields() -> None:
    timeline: list = []
    append_thinking(timeline, "x")
    append_tool(timeline, "echo", "ok", '{"q": 1}')
    append_thinking(timeline, "y")
    act = activity_from_timeline(timeline, thinking_cap=20_000)
    # flat fields are derived from the ordered timeline (back-compat)
    assert act.thinking == "xy"
    assert [s.tool for s in act.steps] == ["echo"]
    assert act.steps[0].detail == '{"q": 1}'
    assert len(act.timeline) == 3


def test_thinking_cap_truncates_derived_field() -> None:
    timeline: list = []
    append_thinking(timeline, "z" * 50)
    act = activity_from_timeline(timeline, thinking_cap=10)
    assert act.thinking == "z" * 10


def test_old_record_without_timeline_round_trips() -> None:
    # A row persisted before the timeline existed: thinking/steps present, no timeline.
    act = MessageActivity.model_validate(
        {"thinking": "hi", "steps": [{"tool": "echo", "status": "ok"}]}
    )
    assert act.timeline == []
    assert act.thinking == "hi"
    assert [s.tool for s in act.steps] == ["echo"]
    assert not act.is_empty()
