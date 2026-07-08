"""Unit tests for the mail MCP tool surface (provider mocked)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from epicurus_core.contracts import DraftReview, ToolEnvelope
from epicurus_mail.provider import ComposedMessage, MailMessage, MailProvider
from epicurus_mail.service import build_module


def _make_provider(*messages: MailMessage) -> MailProvider:
    provider = AsyncMock(spec=MailProvider)
    provider.search = AsyncMock(return_value=list(messages))
    provider.read = AsyncMock(return_value=messages[0] if messages else _sample())
    # Draft-first (ADR-0085): mail_reply composes via compose_reply; no tool transmits. transmit()
    # exists only for the /send endpoint and must never be reached from a tool call.
    provider.compose_reply = AsyncMock(
        return_value=ComposedMessage(
            to="alice@example.com", subject="Re: Hello", body="", thread_id="thread1"
        )
    )
    provider.transmit = AsyncMock(return_value="sent_msg_id")
    provider.set_unread = AsyncMock(return_value=None)
    return provider  # type: ignore[return-value]


def _sample() -> MailMessage:
    return MailMessage(
        id="msg1",
        thread_id="thread1",
        subject="Hello",
        sender="alice@example.com",
        to=["bob@example.com"],
        date="Mon, 1 Jan 2024 10:00:00 +0000",
        snippet="Hey there",
        body="Full body text",
    )


def _parse_envelope(content: list) -> ToolEnvelope:  # type: ignore[type-arg]
    """Extract the ToolEnvelope from the first TextContent item in a call_tool result."""
    text = content[0].text  # type: ignore[attr-defined]
    return ToolEnvelope.model_validate_json(text)


def _parse_draft(content: list) -> DraftReview:  # type: ignore[type-arg]
    """Extract the DraftReview envelope from the first TextContent item (ADR-0085)."""
    text = content[0].text  # type: ignore[attr-defined]
    return DraftReview.model_validate_json(text)


async def test_mail_search_returns_entity_refs() -> None:
    provider = _make_provider(_sample())
    module = build_module(provider)
    content, _ = await module.mcp.call_tool("mail_search", {"query": "from:alice"})
    envelope = _parse_envelope(content)
    assert len(envelope.entity_refs) == 1
    ref = envelope.entity_refs[0]
    assert ref.ref_id == "msg1"
    assert ref.module == "mail"
    assert ref.kind == "message"
    assert ref.title == "Hello"
    assert ref.summary == "Hey there"


async def test_mail_search_text_summary_mentions_count() -> None:
    provider = _make_provider(_sample())
    module = build_module(provider)
    content, _ = await module.mcp.call_tool("mail_search", {"query": "from:alice"})
    envelope = _parse_envelope(content)
    assert "1" in envelope.text
    assert "Hello" in envelope.text


async def test_mail_search_empty_returns_no_refs() -> None:
    provider = AsyncMock(spec=MailProvider)
    provider.search = AsyncMock(return_value=[])
    provider.read = AsyncMock(return_value=_sample())
    module = build_module(provider)  # type: ignore[arg-type]
    content, _ = await module.mcp.call_tool("mail_search", {"query": "nothing"})
    envelope = _parse_envelope(content)
    assert envelope.entity_refs == []
    assert "No messages" in envelope.text


async def test_mail_search_caps_at_50() -> None:
    provider = _make_provider()
    module = build_module(provider)
    await module.mcp.call_tool("mail_search", {"query": "x", "max_results": 999})
    provider.search.assert_called_once_with("x", 50)  # type: ignore[attr-defined]


async def test_mail_search_clamps_min_to_1() -> None:
    provider = _make_provider()
    module = build_module(provider)
    await module.mcp.call_tool("mail_search", {"query": "x", "max_results": 0})
    provider.search.assert_called_once_with("x", 1)  # type: ignore[attr-defined]


async def test_mail_read_returns_formatted_message() -> None:
    provider = _make_provider(_sample())
    module = build_module(provider)
    content, _ = await module.mcp.call_tool("mail_read", {"message_id": "msg1"})
    text = content[0].text  # type: ignore[attr-defined]
    assert "Subject: Hello" in text
    assert "From: alice@example.com" in text
    assert "Full body text" in text
    provider.read.assert_called_once_with("msg1")  # type: ignore[attr-defined]


# ── mail_send / mail_reply: compose-only, never transmit (ADR-0085) ───────────


async def test_mail_send_composes_a_draft_and_does_not_send() -> None:
    provider = _make_provider(_sample())
    module = build_module(provider)
    content, _ = await module.mcp.call_tool(
        "mail_send", {"to": "bob@example.com", "subject": "Hi", "body": "Hello!"}
    )
    draft = _parse_draft(content)
    assert draft.kind == "mail"
    assert draft.module == "mail"
    assert draft.draft["to"] == "bob@example.com"
    assert draft.draft["subject"] == "Hi"
    assert draft.draft["body"] == "Hello!"
    # The structural guarantee (ADR-0085): composing never transmits.
    provider.transmit.assert_not_called()  # type: ignore[attr-defined]


async def test_mail_send_rejects_a_blank_recipient() -> None:
    provider = _make_provider(_sample())
    module = build_module(provider)
    content, _ = await module.mcp.call_tool(
        "mail_send", {"to": "   ", "subject": "Hi", "body": "Hello!"}
    )
    text = content[0].text  # type: ignore[attr-defined]
    assert "recipient" in str(text)
    provider.transmit.assert_not_called()  # type: ignore[attr-defined]


async def test_mail_reply_composes_a_draft_via_compose_reply_and_does_not_send() -> None:
    provider = _make_provider(_sample())
    module = build_module(provider)
    content, _ = await module.mcp.call_tool(
        "mail_reply", {"message_id": "msg1", "body": "Sounds good!"}
    )
    draft = _parse_draft(content)
    assert draft.kind == "mail"
    assert draft.module == "mail"
    assert draft.draft["to"] == "alice@example.com"
    assert draft.draft["subject"] == "Re: Hello"
    assert draft.draft["thread_id"] == "thread1"
    provider.compose_reply.assert_called_once_with(  # type: ignore[attr-defined]
        "msg1", "Sounds good!"
    )
    provider.transmit.assert_not_called()  # type: ignore[attr-defined]


async def test_no_mcp_tool_transmits() -> None:
    # The structural guarantee (ADR-0085): calling every tool the module exposes never reaches
    # provider.transmit — there is no MCP tool that sends. Only the /send HTTP endpoint does.
    provider = _make_provider(_sample())
    module = build_module(provider)
    await module.mcp.call_tool("mail_search", {"query": "x"})
    await module.mcp.call_tool("mail_read", {"message_id": "msg1"})
    await module.mcp.call_tool("mail_send", {"to": "b@x.com", "subject": "s", "body": "b"})
    await module.mcp.call_tool("mail_reply", {"message_id": "msg1", "body": "b"})
    await module.mcp.call_tool("mail_mark_read", {"message_id": "msg1"})
    await module.mcp.call_tool("mail_mark_unread", {"message_id": "msg1"})
    provider.transmit.assert_not_called()  # type: ignore[attr-defined]


# ── mark read / unread ─────────────────────────────────────────────────────────


async def test_mail_mark_read_clears_unread() -> None:
    provider = _make_provider(_sample())
    module = build_module(provider)
    content, _ = await module.mcp.call_tool("mail_mark_read", {"message_id": "msg1"})
    text = content[0].text  # type: ignore[attr-defined]
    assert "marked-read:msg1" in str(text)
    provider.set_unread.assert_called_once_with("msg1", unread=False)  # type: ignore[attr-defined]


async def test_mail_mark_unread_sets_unread() -> None:
    provider = _make_provider(_sample())
    module = build_module(provider)
    content, _ = await module.mcp.call_tool("mail_mark_unread", {"message_id": "msg1"})
    text = content[0].text  # type: ignore[attr-defined]
    assert "marked-unread:msg1" in str(text)
    provider.set_unread.assert_called_once_with("msg1", unread=True)  # type: ignore[attr-defined]


async def test_mail_mark_read_returns_hint_on_missing_scope() -> None:
    # A 403 from Gmail (token lacks gmail.modify) returns a reconnect hint, not a 500.
    provider = _make_provider(_sample())
    provider.set_unread = AsyncMock(  # type: ignore[method-assign]
        side_effect=httpx.HTTPStatusError(
            "403 Forbidden",
            request=httpx.Request("POST", "http://gmail/modify"),
            response=httpx.Response(403),
        )
    )
    module = build_module(provider)
    content, _ = await module.mcp.call_tool("mail_mark_read", {"message_id": "msg1"})
    text = content[0].text  # type: ignore[attr-defined]
    assert "Reconnect Google" in str(text)


async def test_mail_mark_read_403_rate_limit_reason_is_not_mislabeled_as_scope() -> None:
    # Gmail also 403s for throttling, not just a missing scope (#538) — a rate-limit reason must
    # surface a "try again" hint, not the misleading "reconnect Google" one.
    provider = _make_provider(_sample())
    provider.set_unread = AsyncMock(  # type: ignore[method-assign]
        side_effect=httpx.HTTPStatusError(
            "403 Forbidden",
            request=httpx.Request("POST", "http://gmail/modify"),
            response=httpx.Response(
                403, json={"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}
            ),
        )
    )
    module = build_module(provider)
    content, _ = await module.mcp.call_tool("mail_mark_read", {"message_id": "msg1"})
    text = content[0].text  # type: ignore[attr-defined]
    assert "rate-limiting" in str(text)
    assert "Reconnect Google" not in str(text)


async def test_mail_reply_returns_lookup_hint_on_missing_scope() -> None:
    # mail_reply's only Gmail call is now the compose-time metadata GET (needs gmail.modify); a
    # 403 there returns the reconnect *lookup* hint — reply never sends, so it is never a send
    # hint (#513/#538, ADR-0085). The turn is not paused (no draft), so nothing is transmitted.
    provider = _make_provider(_sample())
    provider.compose_reply = AsyncMock(  # type: ignore[method-assign]
        side_effect=httpx.HTTPStatusError(
            "403 Forbidden",
            request=httpx.Request("GET", "http://gmail/users/me/messages/msg1"),
            response=httpx.Response(403),
        )
    )
    module = build_module(provider)
    content, _ = await module.mcp.call_tool(
        "mail_reply", {"message_id": "msg1", "body": "Sounds good!"}
    )
    text = content[0].text  # type: ignore[attr-defined]
    assert "look up the original message" in str(text)
    provider.transmit.assert_not_called()  # type: ignore[attr-defined]


async def test_mail_reply_reraises_non_scope_errors() -> None:
    # A non-403 HTTP error from the compose lookup must not be swallowed into a false hint.
    provider = _make_provider(_sample())
    provider.compose_reply = AsyncMock(  # type: ignore[method-assign]
        side_effect=httpx.HTTPStatusError(
            "500 Server Error",
            request=httpx.Request("GET", "http://gmail/users/me/messages/msg1"),
            response=httpx.Response(500),
        )
    )
    module = build_module(provider)
    with pytest.raises(Exception, match="500"):
        await module.mcp.call_tool("mail_reply", {"message_id": "msg1", "body": "Sounds good!"})


async def test_mail_search_uses_capped_listing_format() -> None:
    # mail_search's listing text goes through the shared capped_listing helper (#539),
    # matching calendar's #522 adoption instead of hand-rolling its own "Found N ...:" text.
    provider = _make_provider(_sample())
    module = build_module(provider)
    content, _ = await module.mcp.call_tool("mail_search", {"query": "from:alice"})
    envelope = _parse_envelope(content)
    expected_line = "- [Hello] from alice@example.com (Mon, 1 Jan 2024 10:00:00 +0000)"
    assert envelope.text == f"Found 1 message(s):\n{expected_line}"


# ── manifest ───────────────────────────────────────────────────────────────────


async def test_manifest_declares_all_tools() -> None:
    provider = _make_provider()
    module = build_module(provider)
    manifest = await module.manifest()
    tool_names = {t.name for t in manifest.tools}
    assert tool_names == {
        "mail_search",
        "mail_read",
        "mail_send",
        "mail_reply",
        "mail_mark_read",
        "mail_mark_unread",
    }


async def test_manifest_has_ui_with_status_url() -> None:
    provider = _make_provider()
    module = build_module(provider)
    manifest = await module.manifest()
    assert manifest.ui is not None
    assert manifest.ui.status_url == "/status"
    assert manifest.ui.icon == "mail"


async def test_manifest_mail_send_and_reply_are_compose_actions_not_danger() -> None:
    # Draft-first (ADR-0085): the send/reply actions now compose a draft for review, so they are
    # no longer one-tap danger sends — nothing on the manifest surface delivers mail directly.
    provider = _make_provider()
    module = build_module(provider)
    manifest = await module.manifest()
    assert manifest.ui is not None
    actions = {a.tool: a for a in manifest.ui.actions}
    assert set(actions) == {"mail_send", "mail_reply"}
    assert all(a.intent != "danger" for a in actions.values())
    assert all(a.confirm is None for a in actions.values())


async def test_manifest_emits_mail_sent_event() -> None:
    provider = _make_provider()
    module = build_module(provider)
    manifest = await module.manifest()
    subjects = {e.subject for e in manifest.events_emitted}
    assert "mail.sent" in subjects


async def test_manifest_declares_resolver() -> None:
    provider = _make_provider()
    module = build_module(provider)
    manifest = await module.manifest()
    assert manifest.resolver is True


async def test_manifest_version_is_0_9_0() -> None:
    provider = _make_provider()
    module = build_module(provider)
    manifest = await module.manifest()
    assert manifest.version == "0.9.0"


async def test_manifest_declares_gmail_oauth_scopes() -> None:
    # The Gmail API scopes the shell requests at connect (#241); identity is the core default.
    # ``gmail.modify`` (not ``readonly``) backs read + mark read/unread (#277).
    provider = _make_provider()
    manifest = await build_module(provider).manifest()
    assert manifest.oauth_scopes == {
        "google": [
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.send",
        ]
    }
