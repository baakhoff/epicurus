"""Tests for the messaging inbox contract (ADR-0058)."""

from __future__ import annotations

import epicurus_core
from epicurus_core import (
    MESSAGING_INBOUND,
    MESSAGING_OUTBOUND,
    InboundMessage,
    MessageAttachment,
    OutboundMessage,
    scope_subject,
    session_id_for,
)


def test_subjects_are_stable_and_tenant_scopable() -> None:
    # The base subjects are the documented contract; tenant scoping wraps them.
    assert MESSAGING_INBOUND == "messaging.inbound"
    assert MESSAGING_OUTBOUND == "messaging.outbound"
    assert scope_subject(MESSAGING_INBOUND, "local") == "local.messaging.inbound"
    assert scope_subject(MESSAGING_OUTBOUND, "acme") == "acme.messaging.outbound"


def test_contract_is_exported() -> None:
    for name in (
        "MESSAGING_INBOUND",
        "MESSAGING_OUTBOUND",
        "InboundMessage",
        "MessageAttachment",
        "OutboundMessage",
        "session_id_for",
    ):
        assert name in epicurus_core.__all__


def test_session_id_for_with_thread() -> None:
    assert session_id_for("telegram", "12345", "678") == "telegram:12345:678"


def test_session_id_for_without_thread() -> None:
    # No thread → the channel's main timeline is one session.
    assert session_id_for("discord", "general") == "discord:general"
    assert session_id_for("discord", "general", None) == "discord:general"


def test_inbound_message_session_id_matches_helper() -> None:
    with_thread = InboundMessage(tenant="local", bridge="telegram", channel_id="c1", thread_id="t1")
    assert with_thread.session_id() == session_id_for("telegram", "c1", "t1") == "telegram:c1:t1"
    no_thread = InboundMessage(tenant="local", bridge="loopback", channel_id="c1")
    assert no_thread.session_id() == "loopback:c1"


def test_inbound_message_defaults_and_required_fields() -> None:
    msg = InboundMessage(tenant="local", bridge="loopback", channel_id="c1")
    assert msg.tenant == "local"
    assert msg.thread_id is None
    assert msg.text == ""
    assert msg.attachments == []
    assert msg.provider_msg_id == ""


def test_inbound_message_round_trips_with_attachments() -> None:
    msg = InboundMessage(
        tenant="local",
        bridge="telegram",
        channel_id="c1",
        thread_id="t1",
        sender_id="u1",
        sender_name="Ada",
        text="hello",
        attachments=[MessageAttachment(kind="image", url="tg://file/abc", name="cat.png")],
        provider_msg_id="m1",
    )
    restored = InboundMessage.model_validate(msg.model_dump())
    assert restored == msg
    assert restored.attachments[0].kind == "image"


def test_outbound_message_round_trips() -> None:
    msg = OutboundMessage(
        tenant="local",
        bridge="telegram",
        channel_id="c1",
        thread_id="t1",
        text="hi back",
        reply_to_msg_id="m1",
    )
    restored = OutboundMessage.model_validate(msg.model_dump())
    assert restored == msg


def test_outbound_message_minimal() -> None:
    msg = OutboundMessage(tenant="local", bridge="loopback", channel_id="c1", text="ok")
    assert msg.thread_id is None
    assert msg.reply_to_msg_id is None
