"""Unit tests for the mail MCP tool surface (provider mocked)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from epicurus_mail.provider import MailMessage, MailProvider
from epicurus_mail.service import build_module


def _make_provider(*messages: MailMessage) -> MailProvider:
    provider = AsyncMock(spec=MailProvider)
    provider.search = AsyncMock(return_value=list(messages))
    provider.read = AsyncMock(return_value=messages[0] if messages else _sample())
    provider.send = AsyncMock(return_value="sent_msg_id")
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


async def test_mail_search_returns_results() -> None:
    provider = _make_provider(_sample())
    module = build_module(provider)
    _content, structured = await module.mcp.call_tool("mail_search", {"query": "from:alice"})
    assert structured is not None
    result = structured.get("result") or structured
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["subject"] == "Hello"


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


async def test_mail_read_returns_message() -> None:
    provider = _make_provider(_sample())
    module = build_module(provider)
    _content, structured = await module.mcp.call_tool("mail_read", {"message_id": "msg1"})
    assert structured is not None
    result = structured.get("result") or structured
    assert result["id"] == "msg1"
    assert result["body"] == "Full body text"
    provider.read.assert_called_once_with("msg1")  # type: ignore[attr-defined]


async def test_mail_send_returns_sent_id() -> None:
    provider = _make_provider(_sample())
    module = build_module(provider)
    _content, structured = await module.mcp.call_tool(
        "mail_send", {"to": "bob@example.com", "subject": "Hi", "body": "Hello!"}
    )
    assert structured is not None
    result = structured.get("result") or structured
    assert "sent:" in str(result)
    provider.send.assert_called_once_with(  # type: ignore[attr-defined]
        to="bob@example.com", subject="Hi", body="Hello!"
    )


async def test_manifest_declares_three_tools() -> None:
    provider = _make_provider()
    module = build_module(provider)
    manifest = await module.manifest()
    tool_names = {t.name for t in manifest.tools}
    assert tool_names == {"mail_search", "mail_read", "mail_send"}


async def test_manifest_has_ui_with_status_url() -> None:
    provider = _make_provider()
    module = build_module(provider)
    manifest = await module.manifest()
    assert manifest.ui is not None
    assert manifest.ui.status_url == "/status"
    assert manifest.ui.icon == "mail"


async def test_manifest_mail_send_is_danger_action() -> None:
    provider = _make_provider()
    module = build_module(provider)
    manifest = await module.manifest()
    assert manifest.ui is not None
    danger = [a for a in manifest.ui.actions if a.intent == "danger"]
    assert len(danger) == 1
    assert danger[0].tool == "mail_send"
    assert danger[0].confirm is not None


async def test_manifest_emits_mail_sent_event() -> None:
    provider = _make_provider()
    module = build_module(provider)
    manifest = await module.manifest()
    subjects = {e.subject for e in manifest.events_emitted}
    assert "mail.sent" in subjects
