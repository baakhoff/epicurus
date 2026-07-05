"""Unit tests for the mail MCP tool surface (provider mocked)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from epicurus_core.contracts import ToolEnvelope
from epicurus_mail.provider import MailMessage, MailProvider
from epicurus_mail.service import build_module


def _make_provider(*messages: MailMessage) -> MailProvider:
    provider = AsyncMock(spec=MailProvider)
    provider.search = AsyncMock(return_value=list(messages))
    provider.read = AsyncMock(return_value=messages[0] if messages else _sample())
    provider.send = AsyncMock(return_value="sent_msg_id")
    provider.reply = AsyncMock(return_value="reply_msg_id")
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
    provider.send = AsyncMock(return_value="x")
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


async def test_mail_send_returns_sent_id() -> None:
    provider = _make_provider(_sample())
    module = build_module(provider)
    content, _ = await module.mcp.call_tool(
        "mail_send", {"to": "bob@example.com", "subject": "Hi", "body": "Hello!"}
    )
    text = content[0].text  # type: ignore[attr-defined]
    assert "sent:" in str(text)
    provider.send.assert_called_once_with(  # type: ignore[attr-defined]
        to="bob@example.com", subject="Hi", body="Hello!"
    )


async def test_mail_reply_returns_sent_id() -> None:
    provider = _make_provider(_sample())
    module = build_module(provider)
    content, _ = await module.mcp.call_tool(
        "mail_reply", {"message_id": "msg1", "body": "Sounds good!"}
    )
    text = content[0].text  # type: ignore[attr-defined]
    assert "sent:" in str(text)
    provider.reply.assert_called_once_with("msg1", "Sounds good!")  # type: ignore[attr-defined]


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


async def test_mail_send_returns_hint_on_missing_scope() -> None:
    # A 403 from Gmail (token lacks gmail.send) returns a reconnect hint, not a raw
    # exception (#513) — the same treatment mail_mark_read/unread already get for
    # gmail.modify.
    provider = _make_provider(_sample())
    provider.send = AsyncMock(  # type: ignore[method-assign]
        side_effect=httpx.HTTPStatusError(
            "403 Forbidden",
            request=httpx.Request("POST", "http://gmail/send"),
            response=httpx.Response(403),
        )
    )
    module = build_module(provider)
    content, _ = await module.mcp.call_tool(
        "mail_send", {"to": "bob@example.com", "subject": "Hi", "body": "Hello!"}
    )
    text = content[0].text  # type: ignore[attr-defined]
    assert "Reconnect Google" in str(text)


async def test_mail_reply_returns_hint_on_missing_scope() -> None:
    # Same scope-hint treatment for reply() (#513).
    provider = _make_provider(_sample())
    provider.reply = AsyncMock(  # type: ignore[method-assign]
        side_effect=httpx.HTTPStatusError(
            "403 Forbidden",
            request=httpx.Request("POST", "http://gmail/send"),
            response=httpx.Response(403),
        )
    )
    module = build_module(provider)
    content, _ = await module.mcp.call_tool(
        "mail_reply", {"message_id": "msg1", "body": "Sounds good!"}
    )
    text = content[0].text  # type: ignore[attr-defined]
    assert "Reconnect Google" in str(text)


@pytest.mark.parametrize(
    "reason", ["rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded"]
)
async def test_mail_send_403_rate_limit_reason_is_not_mislabeled_as_scope(reason: str) -> None:
    # Gmail also 403s for throttling, not just a missing scope (#538) — each rate-limit
    # reason must surface a "try again" hint, not the misleading "reconnect Google" one.
    provider = _make_provider(_sample())
    provider.send = AsyncMock(  # type: ignore[method-assign]
        side_effect=httpx.HTTPStatusError(
            "403 Forbidden",
            request=httpx.Request("POST", "http://gmail/send"),
            response=httpx.Response(403, json={"error": {"errors": [{"reason": reason}]}}),
        )
    )
    module = build_module(provider)
    content, _ = await module.mcp.call_tool(
        "mail_send", {"to": "bob@example.com", "subject": "Hi", "body": "Hello!"}
    )
    text = content[0].text  # type: ignore[attr-defined]
    assert "rate-limiting" in str(text)
    assert "Reconnect Google" not in str(text)


async def test_mail_mark_read_403_rate_limit_reason_is_not_mislabeled_as_scope() -> None:
    # Same distinction must hold for the mark-read/unread pair, not just send (#538).
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


async def test_mail_send_403_with_scope_reason_still_shows_scope_hint() -> None:
    # A genuine scope-denied reason (not a rate-limit one) must still get the reconnect
    # hint — the rate-limit carve-out mustn't swallow real scope errors (#538).
    provider = _make_provider(_sample())
    provider.send = AsyncMock(  # type: ignore[method-assign]
        side_effect=httpx.HTTPStatusError(
            "403 Forbidden",
            request=httpx.Request("POST", "http://gmail/send"),
            response=httpx.Response(
                403, json={"error": {"errors": [{"reason": "insufficientPermissions"}]}}
            ),
        )
    )
    module = build_module(provider)
    content, _ = await module.mcp.call_tool(
        "mail_send", {"to": "bob@example.com", "subject": "Hi", "body": "Hello!"}
    )
    text = content[0].text  # type: ignore[attr-defined]
    assert "Reconnect Google" in str(text)


async def test_mail_reply_403_on_metadata_lookup_names_modify_not_send() -> None:
    # mail_reply's 403 can come from either of two Gmail calls (#538): the metadata GET
    # (needs gmail.modify) or the send POST (needs gmail.send). A 403 whose request path
    # isn't the send endpoint must be attributed to the lookup, not mislabeled as "send".
    provider = _make_provider(_sample())
    provider.reply = AsyncMock(  # type: ignore[method-assign]
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
    assert "Couldn't send" not in str(text)


async def test_mail_reply_403_on_send_names_send_scope() -> None:
    # The complementary case: a 403 from the send POST itself is the send-scope hint.
    provider = _make_provider(_sample())
    provider.reply = AsyncMock(  # type: ignore[method-assign]
        side_effect=httpx.HTTPStatusError(
            "403 Forbidden",
            request=httpx.Request("POST", "http://gmail/users/me/messages/send"),
            response=httpx.Response(403),
        )
    )
    module = build_module(provider)
    content, _ = await module.mcp.call_tool(
        "mail_reply", {"message_id": "msg1", "body": "Sounds good!"}
    )
    text = content[0].text  # type: ignore[attr-defined]
    assert "Couldn't send" in str(text)


async def test_mail_search_uses_capped_listing_format() -> None:
    # mail_search's listing text goes through the shared capped_listing helper (#539),
    # matching calendar's #522 adoption instead of hand-rolling its own "Found N ...:" text.
    provider = _make_provider(_sample())
    module = build_module(provider)
    content, _ = await module.mcp.call_tool("mail_search", {"query": "from:alice"})
    envelope = _parse_envelope(content)
    expected_line = "- [Hello] from alice@example.com (Mon, 1 Jan 2024 10:00:00 +0000)"
    assert envelope.text == f"Found 1 message(s):\n{expected_line}"


async def test_mail_send_reraises_non_scope_errors() -> None:
    # A non-403 HTTP error must not be swallowed into a false "reconnect" hint.
    provider = _make_provider(_sample())
    provider.send = AsyncMock(  # type: ignore[method-assign]
        side_effect=httpx.HTTPStatusError(
            "500 Server Error",
            request=httpx.Request("POST", "http://gmail/send"),
            response=httpx.Response(500),
        )
    )
    module = build_module(provider)
    with pytest.raises(Exception, match="500"):
        await module.mcp.call_tool(
            "mail_send", {"to": "bob@example.com", "subject": "Hi", "body": "Hello!"}
        )


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


async def test_manifest_mail_send_and_reply_are_danger_actions() -> None:
    provider = _make_provider()
    module = build_module(provider)
    manifest = await module.manifest()
    assert manifest.ui is not None
    danger = [a for a in manifest.ui.actions if a.intent == "danger"]
    assert {a.tool for a in danger} == {"mail_send", "mail_reply"}
    assert all(a.confirm is not None for a in danger)


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


async def test_manifest_version_is_0_8_2() -> None:
    provider = _make_provider()
    module = build_module(provider)
    manifest = await module.manifest()
    assert manifest.version == "0.8.2"


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
