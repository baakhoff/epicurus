"""Tests for the module manifest models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from epicurus_core.manifest import (
    CONTRACT_VERSION,
    EventSpec,
    ModuleManifest,
    ToolSpec,
    UiAction,
    UiSection,
)


def test_defaults() -> None:
    m = ModuleManifest(name="greeter", version="1.0.0")
    assert m.contract_version == CONTRACT_VERSION
    assert m.image is None
    assert m.tools == []
    assert m.events_emitted == []
    assert m.secrets == []


def test_roundtrip() -> None:
    m = ModuleManifest(
        name="greeter",
        version="1.0.0",
        description="says hi",
        image="ghcr.io/x/greeter:1",
        tools=[ToolSpec(name="greet", description="greet", input_schema={"type": "object"})],
        events_emitted=[EventSpec(subject="greeting.sent", description="after greeting")],
        events_consumed=[EventSpec(subject="inbox.message")],
        config=["GREETING"],
        secrets=["API_KEY"],
    )
    restored = ModuleManifest.model_validate(m.model_dump())
    assert restored == m
    assert restored.tools[0].name == "greet"
    assert restored.events_emitted[0].subject == "greeting.sent"
    assert restored.secrets == ["API_KEY"]


def test_ui_section_defaults() -> None:
    ui = UiSection()
    assert ui.ui_version == "1"
    assert ui.icon == "puzzle"
    assert ui.config_schema is None
    assert ui.actions == []
    assert ui.ui_url is None


def test_danger_action_requires_a_confirm_prompt() -> None:
    with pytest.raises(ValidationError):
        UiAction(tool="purge", label="Purge", intent="danger")
    # with a confirm prompt it validates
    action = UiAction(tool="purge", label="Purge", intent="danger", confirm="Erase everything?")
    assert action.confirm == "Erase everything?"


def test_non_danger_actions_need_no_confirm() -> None:
    assert UiAction(tool="echo", label="Send", intent="primary").confirm is None
    assert UiAction(tool="echo", label="Send").intent == "default"


def test_manifest_with_ui_roundtrips() -> None:
    m = ModuleManifest(
        name="greeter",
        version="1.0.0",
        ui=UiSection(
            summary="says hi",
            config_schema={"type": "object", "properties": {"greeting": {"type": "string"}}},
            actions=[UiAction(tool="greet", label="Greet")],
        ),
    )
    restored = ModuleManifest.model_validate(m.model_dump())
    assert restored == m
    assert restored.ui is not None
    assert restored.ui.actions[0].tool == "greet"
    assert restored.ui.config_schema is not None
