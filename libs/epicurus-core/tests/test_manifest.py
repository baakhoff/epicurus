"""Tests for the module manifest models."""

from __future__ import annotations

from epicurus_core.manifest import CONTRACT_VERSION, EventSpec, ModuleManifest, ToolSpec


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
