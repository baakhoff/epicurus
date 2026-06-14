"""Tests for the shared chat contract (ADR-0021) — one source of truth."""

from __future__ import annotations

import json

import epicurus_core
from epicurus_core import (
    Attachment,
    ChatMessage,
    ChatResult,
    EntityRef,
    HoverCard,
    PlatformChatResponse,
    PlatformMessage,
    ToolEnvelope,
    tool_envelope,
)


def test_chat_types_are_exported() -> None:
    assert {"ChatMessage", "ChatResult", "Role"} <= set(epicurus_core.__all__)


def test_platform_aliases_are_the_canonical_types() -> None:
    # The historical names are aliases of the canonical contract, not copies —
    # so there is exactly one definition of each shape.
    assert PlatformMessage is ChatMessage
    assert PlatformChatResponse is ChatResult


def test_platform_client_reexports_the_aliases() -> None:
    # ``from epicurus_core.platform_client import PlatformChatResponse`` must keep
    # resolving for existing module code.
    from epicurus_core.platform_client import (
        PlatformChatResponse as ClientResponse,
    )
    from epicurus_core.platform_client import (
        PlatformMessage as ClientMessage,
    )

    assert ClientResponse is ChatResult
    assert ClientMessage is ChatMessage


def test_chat_message_round_trips() -> None:
    msg = ChatMessage(role="user", content="hi")
    assert msg.model_dump(exclude_none=True) == {"role": "user", "content": "hi"}


def test_entity_ref_types_are_exported() -> None:
    assert {"EntityRef", "HoverCard", "ToolEnvelope", "tool_envelope"} <= set(epicurus_core.__all__)


def test_provider_dump_strips_ui_only_fields() -> None:
    # entity_refs + attachments are UI/agent metadata (ADR-0019) — never sent to a provider.
    ref = EntityRef(ref_id="e1", module="calendar", kind="event", title="Standup")
    att = Attachment(att_id="a1", source="file", title="notes.txt")
    msg = ChatMessage(
        role="user", content="summarize my standup", entity_refs=[ref], attachments=[att]
    )
    dumped = msg.provider_dump()
    assert "entity_refs" not in dumped
    assert "attachments" not in dumped
    assert dumped == {"role": "user", "content": "summarize my standup"}


def test_attachment_is_exported_and_defaults() -> None:
    assert "Attachment" in epicurus_core.__all__
    att = Attachment(att_id="a1", source="chat", ref_id="s1", title="earlier chat")
    assert att.kind == ""
    assert att.module is None


def test_chat_message_defaults_to_no_entity_refs() -> None:
    # Optional so it drops out of the default (provider-bound) serialization.
    assert ChatMessage(role="user", content="hi").entity_refs is None


def test_tool_envelope_round_trips() -> None:
    ref = EntityRef(ref_id="e1", module="calendar", kind="event", title="Standup", summary="9am")
    serialized = tool_envelope("Created your event.", [ref])
    data = json.loads(serialized)
    assert data["text"] == "Created your event."
    restored = ToolEnvelope.model_validate(data)
    assert restored.entity_refs[0].ref_id == "e1"
    assert restored.entity_refs[0].summary == "9am"


def test_hover_card_defaults() -> None:
    card = HoverCard(title="Standup")
    assert card.description == ""
    assert card.details == []
    assert card.href is None


def test_chat_result_round_trips() -> None:
    result = ChatResult(model="ollama_chat/llama3.2", content="hello", completion_tokens=2)
    dumped = result.model_dump()
    assert dumped["model"] == "ollama_chat/llama3.2"
    assert dumped["content"] == "hello"
    assert dumped["completion_tokens"] == 2
