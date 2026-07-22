"""Tests for the manifest's automations contract — tool side effects and templates.

Both are additive fields a module declares, and both have a default that matters: an
unannotated tool must read as ``write`` (the restrictive reading), and a module with no
templates must read as "offers none" rather than breaking a core that asks.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from epicurus_core import AutomationTemplate, EpicurusModule, ModuleManifest, ToolSpec


def test_a_tool_defaults_to_write() -> None:
    # Fail closed. An unannotated tool is withheld from a read-only automation rather than
    # trusted by one: a forgotten annotation costs availability, never containment.
    assert ToolSpec(name="mystery").side_effect == "write"


@pytest.mark.parametrize("side_effect", ["read", "propose", "write"])
def test_the_three_classes_are_accepted(side_effect: str) -> None:
    assert ToolSpec(name="t", side_effect=side_effect).side_effect == side_effect  # type: ignore[arg-type]


def test_an_unknown_class_is_rejected() -> None:
    # The vocabulary is closed: a typo'd "reads" must not silently become an unhandled
    # class that no allowance includes, quietly disabling the tool everywhere.
    with pytest.raises(ValidationError):
        ToolSpec(name="t", side_effect="reads")  # type: ignore[arg-type]


async def test_the_decorator_carries_the_class_into_the_manifest() -> None:
    module = EpicurusModule("demo")

    @module.tool(side_effect="read")
    def look() -> str:
        return "ok"

    @module.tool(side_effect="propose")
    def suggest() -> str:
        return "ok"

    @module.tool()
    def unstated() -> str:
        return "ok"

    manifest = await module.manifest()
    classes = {t.name: t.side_effect for t in manifest.tools}
    assert classes == {"look": "read", "suggest": "propose", "unstated": "write"}


async def test_an_explicit_tool_name_is_classified_under_that_name() -> None:
    # The annotation keys on the name FastMCP publishes, not the function's.
    module = EpicurusModule("demo")

    @module.tool(name="published_name", side_effect="read")
    def internal_name() -> str:
        return "ok"

    manifest = await module.manifest()
    assert {t.name: t.side_effect for t in manifest.tools} == {"published_name": "read"}


async def test_a_side_effect_for_an_unregistered_tool_fails_at_build_time() -> None:
    # It matters more than the writes_document version of this check: an annotation that
    # silently missed its tool would demote that tool to "write" and drop it out of every
    # Notify automation's reach — a feature failing shut, invisibly.
    module = EpicurusModule("demo")
    module._side_effects["ghost"] = "read"

    with pytest.raises(ValueError, match="side_effect declared for unregistered tool"):
        await module.manifest()


async def test_a_module_declares_no_templates_by_default() -> None:
    assert (await EpicurusModule("demo").manifest()).automation_templates == []


async def test_templates_reach_the_manifest() -> None:
    module = EpicurusModule(
        "demo",
        automation_templates=[
            AutomationTemplate(
                key="k",
                name="A preset",
                description="d",
                trigger={"module": "demo", "event_type": "demo.thing"},
                prompt="p",
                autonomy="notify",
                sinks=["chat"],
            )
        ],
    )
    templates = (await module.manifest()).automation_templates
    assert [t.key for t in templates] == ["k"]
    assert templates[0].trigger == {"module": "demo", "event_type": "demo.thing"}


def test_a_template_carries_no_enabled_flag() -> None:
    # The contract enforces the product rule by having nothing to set: a module cannot
    # ship a live automation, only a starting point the operator instantiates.
    assert "enabled" not in AutomationTemplate.model_fields


def test_a_template_needs_a_key_and_a_name() -> None:
    with pytest.raises(ValidationError):
        AutomationTemplate(key="", name="x")
    with pytest.raises(ValidationError):
        AutomationTemplate(key="x", name="")


def test_a_templates_trigger_is_loose_on_purpose() -> None:
    # The core owns the trigger vocabulary and validates on instantiation, so a module
    # pinned to an older library cannot break the core's parse by naming a field it has
    # since renamed.
    template = AutomationTemplate(key="k", name="n", trigger={"anything": "goes"})
    assert template.trigger == {"anything": "goes"}


def test_the_manifest_round_trips_the_new_fields() -> None:
    # A core reading a module's manifest over the wire must see both.
    original = ModuleManifest(
        name="demo",
        version="1.0.0",
        tools=[ToolSpec(name="look", side_effect="read")],
        automation_templates=[AutomationTemplate(key="k", name="n")],
    )
    parsed = ModuleManifest.model_validate_json(original.model_dump_json())
    assert parsed.tools[0].side_effect == "read"
    assert parsed.automation_templates[0].key == "k"


def test_an_older_manifest_without_the_fields_still_parses() -> None:
    # Additive: a module built against an older library omits both, and the core reads
    # sensible defaults rather than failing to load it.
    parsed = ModuleManifest.model_validate(
        {"name": "old", "version": "0.1.0", "tools": [{"name": "t"}]}
    )
    assert parsed.tools[0].side_effect == "write"
    assert parsed.automation_templates == []
