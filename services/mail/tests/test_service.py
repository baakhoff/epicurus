"""Unit tests for the mail MCP tool surface (provider mocked)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from epicurus_core.contracts import DraftReview, ToolEnvelope
from epicurus_mail.provider import ComposedMessage, MailMessage, MailProvider
from epicurus_mail.service import (
    _SCOPE_HINT,
    _describe_403,
    _describe_gmail_error,
    build_module,
)


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


# ── AIP-193 error shape + 429 rate limiting (#557) ─────────────────────────────


def _http_error(
    status: int,
    *,
    body: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.HTTPStatusError:
    """A Gmail-style ``httpx.HTTPStatusError`` with the given status/body/headers."""
    kwargs: dict[str, object] = {"headers": headers or {}}
    if body is not None:
        kwargs["json"] = body
    response = httpx.Response(status, **kwargs)  # type: ignore[arg-type]
    return httpx.HTTPStatusError(
        f"{status} error", request=httpx.Request("GET", "http://gmail/x"), response=response
    )


# Both rate-limit shapes (legacy Gmail v1 `errors[].reason`, modern AIP-193 `status`/
# `details[].reason`) and both permission shapes, by the expected outcome (#557).
_LEGACY_RATE = {"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}
_LEGACY_SCOPE = {"error": {"errors": [{"reason": "insufficientPermissions"}]}}
_AIP_RATE_STATUS = {"error": {"status": "RESOURCE_EXHAUSTED", "message": "quota exceeded"}}
_AIP_RATE_DETAIL = {"error": {"code": 403, "details": [{"reason": "RATE_LIMIT_EXCEEDED"}]}}
_AIP_SCOPE = {"error": {"status": "PERMISSION_DENIED", "message": "insufficient permission"}}


@pytest.mark.parametrize(
    ("body", "is_rate_limit"),
    [
        (_LEGACY_RATE, True),
        (_LEGACY_SCOPE, False),
        (_AIP_RATE_STATUS, True),
        (_AIP_RATE_DETAIL, True),
        (_AIP_SCOPE, False),
    ],
)
def test_describe_403_recognizes_both_error_shapes(
    body: dict[str, object], is_rate_limit: bool
) -> None:
    """A 403 in either the legacy or AIP-193 shape maps to the rate-limit hint when it names a
    rate limit, and the scope hint otherwise (#538, #557)."""
    hint = _describe_403(_http_error(403, body=body), _SCOPE_HINT)
    if is_rate_limit:
        assert "rate-limiting" in hint
        assert hint != _SCOPE_HINT
    else:
        assert hint == _SCOPE_HINT


def test_describe_403_non_string_reason_falls_back_to_scope_without_raising() -> None:
    """A non-string `reason` (a nested object in an otherwise well-formed body) must not reach the
    `in _RATE_LIMIT_REASONS` membership test, where an unhashable value would raise TypeError —
    it falls back to the scope hint (#557)."""
    body = {"error": {"errors": [{"reason": {"nested": "object"}}]}}
    assert _describe_403(_http_error(403, body=body), _SCOPE_HINT) == _SCOPE_HINT


def test_describe_403_unparseable_body_falls_back_to_scope() -> None:
    assert _describe_403(_http_error(403), _SCOPE_HINT) == _SCOPE_HINT  # no JSON body


def test_describe_gmail_error_429_is_always_rate_limit() -> None:
    hint = _describe_gmail_error(_http_error(429), _SCOPE_HINT)
    assert hint is not None
    assert "rate-limiting" in hint


def test_describe_gmail_error_429_surfaces_retry_after_seconds() -> None:
    hint = _describe_gmail_error(_http_error(429, headers={"Retry-After": "30"}), _SCOPE_HINT)
    assert hint is not None
    assert "30 seconds" in hint


def test_describe_gmail_error_403_delegates_to_describe_403() -> None:
    hint = _describe_gmail_error(_http_error(403, body=_AIP_RATE_DETAIL), _SCOPE_HINT)
    assert hint is not None
    assert "rate-limiting" in hint


def test_describe_gmail_error_other_status_returns_none() -> None:
    # A 500 isn't one we soften — the caller re-raises it rather than inventing a hint.
    assert _describe_gmail_error(_http_error(500), _SCOPE_HINT) is None


@pytest.mark.parametrize("tool", ["mail_search", "mail_read"])
async def test_mail_read_paths_429_return_rate_limit_hint_not_raw(tool: str) -> None:
    """search/read had no HTTP-error handling at all — a 429 raised a raw traceback. Now both
    return the wait-and-retry hint (#557)."""
    provider = _make_provider(_sample())
    err = _http_error(429)
    provider.search = AsyncMock(side_effect=err)  # type: ignore[method-assign]
    provider.read = AsyncMock(side_effect=err)  # type: ignore[method-assign]
    module = build_module(provider)
    args = {"query": "x"} if tool == "mail_search" else {"message_id": "msg1"}
    content, _ = await module.mcp.call_tool(tool, args)
    assert "rate-limiting" in str(content[0].text)  # type: ignore[attr-defined]


async def test_mail_reply_429_returns_rate_limit_hint() -> None:
    provider = _make_provider(_sample())
    provider.compose_reply = AsyncMock(side_effect=_http_error(429))  # type: ignore[method-assign]
    module = build_module(provider)
    content, _ = await module.mcp.call_tool("mail_reply", {"message_id": "msg1", "body": "hi"})
    assert "rate-limiting" in str(content[0].text)  # type: ignore[attr-defined]
    provider.transmit.assert_not_called()  # type: ignore[attr-defined]


async def test_mail_search_reraises_a_non_rate_limit_http_error() -> None:
    provider = _make_provider(_sample())
    provider.search = AsyncMock(side_effect=_http_error(500))  # type: ignore[method-assign]
    module = build_module(provider)
    with pytest.raises(Exception, match="500"):
        await module.mcp.call_tool("mail_search", {"query": "x"})


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


async def test_manifest_version_is_0_9_1() -> None:
    provider = _make_provider()
    module = build_module(provider)
    manifest = await module.manifest()
    assert manifest.version == "0.9.1"


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
