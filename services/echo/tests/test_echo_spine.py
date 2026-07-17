"""Unit tests for echo's event-spine emitter — the reference emitter every module copies.

echo is where the spine's contract gets demonstrated, so these tests pin the demonstration:
the manifest declares what it emits, the payload stays pointer-shaped, and the dedup key
behaves both ways (fresh per ping by default, caller-controlled when idempotency matters).
"""

from __future__ import annotations

import pytest

from epicurus_core import EventEnvelope
from epicurus_echo.service import ECHO_PINGED, build_module, emit_ping

TENANT = "local"


class _RecordingBus:
    """Captures publishes instead of talking to NATS."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, object], str | None]] = []

    async def publish(self, subject: str, data: object, tenant_id: str | None = None) -> None:
        assert isinstance(data, dict)
        self.published.append((subject, data, tenant_id))

    def envelopes(self) -> list[EventEnvelope]:
        return [EventEnvelope.model_validate(data) for _, data, _ in self.published]


# ── the manifest ─────────────────────────────────────────────────────────────


async def test_manifest_declares_the_emitted_event() -> None:
    # The catalog is generated from this; an emitter that does not declare itself is
    # invisible to an operator browsing what a module can tell them about.
    manifest = await build_module().manifest()
    assert any(e.subject == "events.echo.pinged" for e in manifest.events_emitted)


async def test_manifest_lists_the_ping_tool() -> None:
    manifest = await build_module().manifest()
    assert any(t.name == "echo_ping" for t in manifest.tools)


async def test_ui_offers_the_ping_action() -> None:
    manifest = await build_module().manifest()
    assert manifest.ui is not None
    assert any(a.tool == "echo_ping" for a in manifest.ui.actions)


async def test_the_tools_declare_their_side_effects() -> None:
    # The automations autonomy dial gates on these (ADR-0105), and echo is the reference:
    # `echo` observes, `echo_ping` puts an event on the bus that other things react to.
    manifest = await build_module().manifest()
    classes = {t.name: t.side_effect for t in manifest.tools}
    assert classes["echo"] == "read"
    assert classes["echo_ping"] == "write"


async def test_an_unannotated_tool_would_default_to_write() -> None:
    # Fail closed: forgetting to annotate costs a tool its availability to a Notify
    # automation, never the guarantee.
    from epicurus_core import ToolSpec

    assert ToolSpec(name="whatever").side_effect == "write"


async def test_the_manifest_declares_an_automation_template() -> None:
    # The Templates-tab contract. Declaring it creates nothing — the operator
    # instantiates it — so installing echo never starts an automation on its own.
    manifest = await build_module().manifest()
    template = next(t for t in manifest.automation_templates if t.key == "on-ping")
    assert template.trigger == {"module": "echo", "event_type": ECHO_PINGED}
    assert template.autonomy == "notify"
    assert template.sinks == ["chat"]
    # A template carries no "enabled" — there is nothing for a module to switch on.
    assert not hasattr(template, "enabled")


# ── emit_ping ────────────────────────────────────────────────────────────────


async def test_emit_ping_publishes_a_valid_envelope() -> None:
    bus = _RecordingBus()
    key = await emit_ping(bus, tenant=TENANT, note="hello")  # type: ignore[arg-type]
    subject, _data, tenant = bus.published[0]
    assert subject == "events.echo.pinged"
    assert tenant == TENANT
    envelope = bus.envelopes()[0]
    assert envelope.type == ECHO_PINGED
    assert envelope.module == "echo"
    assert envelope.dedup_key == key
    assert envelope.payload["note"] == "hello"


async def test_emit_ping_carries_an_entity_ref() -> None:
    # ADR-0019: the feed renders a hover-card chip with no echo-specific code in the shell.
    bus = _RecordingBus()
    key = await emit_ping(bus, tenant=TENANT, note="hi")  # type: ignore[arg-type]
    ref = bus.envelopes()[0].entity_ref
    assert ref is not None
    assert ref.module == "echo"
    assert ref.kind == "ping"
    assert ref.ref_id == key


async def test_each_ping_is_its_own_event_by_default() -> None:
    # A fresh key per ping is correct *here*: two pings are two changes. Every other
    # emitter derives its key from the change it re-sees.
    bus = _RecordingBus()
    first = await emit_ping(bus, tenant=TENANT)  # type: ignore[arg-type]
    second = await emit_ping(bus, tenant=TENANT)  # type: ignore[arg-type]
    assert first != second


async def test_an_explicit_dedup_key_is_honoured() -> None:
    # This is what lets the demo surface (and the smoke gate) prove the log's idempotency.
    bus = _RecordingBus()
    key = await emit_ping(bus, tenant=TENANT, dedup_key="fixed")  # type: ignore[arg-type]
    assert key == "fixed"
    assert bus.envelopes()[0].dedup_key == "fixed"


async def test_a_long_note_is_truncated_to_stay_a_pointer() -> None:
    # echo models the discipline it demonstrates: the payload names what changed, it does
    # not carry content.
    bus = _RecordingBus()
    await emit_ping(bus, tenant=TENANT, note="x" * 5000)  # type: ignore[arg-type]
    assert len(bus.envelopes()[0].payload["note"]) == 200


async def test_an_empty_note_is_omitted_entirely() -> None:
    bus = _RecordingBus()
    await emit_ping(bus, tenant=TENANT)  # type: ignore[arg-type]
    assert bus.envelopes()[0].payload == {}


async def test_the_payload_never_repeats_an_envelope_field() -> None:
    # Two reasons, one test: it is redundant (dedup_key is carried on the envelope), and
    # the payload's credential screen matches "key" by substring, so a payload field named
    # dedup_key would be refused outright.
    bus = _RecordingBus()
    await emit_ping(bus, tenant=TENANT, note="hi", dedup_key="fixed")  # type: ignore[arg-type]
    envelope = bus.envelopes()[0]
    assert envelope.dedup_key == "fixed"
    assert "dedup_key" not in envelope.payload


async def test_emit_ping_rejects_a_malformed_tenant() -> None:
    # The envelope's tenant rule reaches all the way out to the module's call site.
    bus = _RecordingBus()
    with pytest.raises(ValueError, match="invalid tenant"):
        await emit_ping(bus, tenant="NOT VALID")  # type: ignore[arg-type]
    assert bus.published == []


# ── the tool surface ─────────────────────────────────────────────────────────


async def test_ping_tool_emits_and_reports_the_key() -> None:
    bus = _RecordingBus()
    module = build_module(bus, tenant=TENANT)  # type: ignore[arg-type]
    _content, structured = await module.mcp.call_tool("echo_ping", {"note": "hi"})
    key = bus.envelopes()[0].dedup_key
    assert isinstance(structured, dict)
    assert key in str(structured["result"])
    assert len(bus.published) == 1


async def test_ping_tool_forwards_an_explicit_dedup_key() -> None:
    bus = _RecordingBus()
    module = build_module(bus, tenant=TENANT)  # type: ignore[arg-type]
    await module.mcp.call_tool("echo_ping", {"dedup_key": "fixed"})
    await module.mcp.call_tool("echo_ping", {"dedup_key": "fixed"})
    # Both go on the wire — deduplication is the core log's job, not the emitter's.
    assert [e.dedup_key for e in bus.envelopes()] == ["fixed", "fixed"]


async def test_ping_tool_reports_a_missing_bus_instead_of_failing_the_build() -> None:
    # build_module() with no bus stays valid so the manifest is readable without NATS.
    _content, structured = await build_module().mcp.call_tool("echo_ping", {})
    assert isinstance(structured, dict)
    assert "error" in str(structured["result"])


async def test_echo_tool_still_works_alongside_the_emitter() -> None:
    # The spine is additive: the original contract proof is untouched.
    _content, structured = await build_module().mcp.call_tool("echo", {"message": "hello"})
    assert structured == {"result": "hello"}
