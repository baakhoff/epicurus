"""Unit tests for GmailProvider helpers and the MailMessage parser."""

from __future__ import annotations

import base64
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from epicurus_core import PlatformClient
from epicurus_mail.gmail import GmailProvider, _extract_body, _parse_message


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _make_platform(access_token: str = "tok") -> PlatformClient:
    platform = MagicMock(spec=PlatformClient)
    platform.get_oauth_token = AsyncMock(return_value=access_token)
    return platform  # type: ignore[return-value]


def _gmail_msg(
    msg_id: str = "m1",
    subject: str = "Test",
    sender: str = "alice@example.com",
    to: str = "bob@example.com",
    body_text: str | None = None,
    label_ids: list[str] | None = None,
) -> dict[str, Any]:
    headers = [
        {"name": "Subject", "value": subject},
        {"name": "From", "value": sender},
        {"name": "To", "value": to},
        {"name": "Date", "value": "Mon, 1 Jan 2024 10:00:00 +0000"},
    ]
    if body_text is not None:
        payload: dict[str, Any] = {
            "headers": headers,
            "mimeType": "text/plain",
            "body": {"data": _b64(body_text)},
        }
    else:
        payload = {"headers": headers, "mimeType": "multipart/alternative", "parts": []}
    message: dict[str, Any] = {
        "id": msg_id,
        "threadId": "t1",
        "snippet": "Snippet…",
        "payload": payload,
    }
    if label_ids is not None:
        message["labelIds"] = label_ids
    return message


# ── _extract_body ────────────────────────────────────────────────────────────


def test_extract_body_plain_text() -> None:
    payload = {"mimeType": "text/plain", "body": {"data": _b64("Hello, world!")}}
    assert _extract_body(payload) == "Hello, world!"


def test_extract_body_from_parts() -> None:
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64("plain text")}},
            {"mimeType": "text/html", "body": {"data": _b64("<b>html</b>")}},
        ],
    }
    assert _extract_body(payload) == "plain text"


def test_extract_body_returns_none_for_non_text() -> None:
    payload = {"mimeType": "text/html", "body": {"data": _b64("<b>html</b>")}}
    assert _extract_body(payload) is None


def test_extract_body_empty_payload() -> None:
    assert _extract_body({}) is None


# ── _parse_message ────────────────────────────────────────────────────────────


def test_parse_message_metadata() -> None:
    data = _gmail_msg("m1", subject="Re: Hello", sender="alice@example.com", to="bob@example.com")
    msg = _parse_message(data, full=False)
    assert msg.id == "m1"
    assert msg.thread_id == "t1"
    assert msg.subject == "Re: Hello"
    assert msg.sender == "alice@example.com"
    assert msg.to == ["bob@example.com"]
    assert msg.body is None


def test_parse_message_full_with_body() -> None:
    data = _gmail_msg("m2", body_text="Plain body here.")
    msg = _parse_message(data, full=True)
    assert msg.id == "m2"
    assert msg.body == "Plain body here."


def test_parse_message_missing_headers_safe() -> None:
    data = {"id": "m3", "threadId": "t3", "snippet": "", "payload": {"headers": []}}
    msg = _parse_message(data, full=False)
    assert msg.subject == "(no subject)"
    assert msg.sender == ""
    assert msg.to == []


def test_parse_message_multiple_recipients() -> None:
    data = _gmail_msg(to="alice@example.com, bob@example.com, carol@example.com")
    msg = _parse_message(data, full=False)
    assert msg.to == ["alice@example.com", "bob@example.com", "carol@example.com"]


def test_parse_message_unread_from_label() -> None:
    data = _gmail_msg(label_ids=["INBOX", "UNREAD"])
    msg = _parse_message(data, full=False)
    assert msg.unread is True


def test_parse_message_read_without_unread_label() -> None:
    data = _gmail_msg(label_ids=["INBOX"])
    msg = _parse_message(data, full=False)
    assert msg.unread is False


def test_parse_message_unread_defaults_false_without_labels() -> None:
    # A response with no labelIds is treated as read (no UNREAD flag present).
    data = _gmail_msg()
    msg = _parse_message(data, full=False)
    assert msg.unread is False


# ── GmailProvider (httpx patched via provider internals) ─────────────────────


async def test_health_check_returns_false_when_not_connected() -> None:
    platform = MagicMock(spec=PlatformClient)
    platform.get_oauth_token = AsyncMock(side_effect=Exception("not connected"))
    provider = GmailProvider(platform=platform, tenant_id="local")  # type: ignore[arg-type]
    assert await provider.health_check() is False


async def test_is_available_true_when_token_present() -> None:
    # A fast token-presence check (#209) — no live Gmail call.
    platform = _make_platform("tok")
    provider = GmailProvider(platform=platform, tenant_id="local")  # type: ignore[arg-type]
    assert await provider.is_available() is True


async def test_is_available_false_on_http_error() -> None:
    # Not connected (4xx) or the core unreachable both read as "not available".
    platform = MagicMock(spec=PlatformClient)
    platform.get_oauth_token = AsyncMock(side_effect=httpx.ConnectError("core down"))
    provider = GmailProvider(platform=platform, tenant_id="local")  # type: ignore[arg-type]
    assert await provider.is_available() is False


async def test_get_token_uses_platform_client() -> None:
    platform = _make_platform("my_access_token")
    provider = GmailProvider(platform=platform, tenant_id="local")  # type: ignore[arg-type]
    token = await provider._get_token()
    assert token == "my_access_token"
    platform.get_oauth_token.assert_called_once_with("google")  # type: ignore[attr-defined]


async def test_search_fetches_token_and_calls_list() -> None:
    """search() fetches a token then calls the Gmail list API."""
    platform = _make_platform("tok123")
    provider = GmailProvider(platform=platform, tenant_id="local")  # type: ignore[arg-type]

    # Stub _list_message_ids and _fetch_message so no real HTTP is made.
    list_mock = AsyncMock(return_value=["m1"])
    fetch_mock = AsyncMock(return_value=_parse_message(_gmail_msg("m1"), full=False))

    provider._list_message_ids = list_mock  # type: ignore[method-assign]
    provider._fetch_message = fetch_mock  # type: ignore[method-assign]

    results = await provider.search("from:alice", max_results=5)
    assert len(results) == 1
    assert results[0].id == "m1"
    platform.get_oauth_token.assert_called_once_with("google")  # type: ignore[attr-defined]


async def test_read_fetches_token_and_full_message() -> None:
    platform = _make_platform("tok456")
    provider = GmailProvider(platform=platform, tenant_id="local")  # type: ignore[arg-type]

    full_msg = _parse_message(_gmail_msg("m2", body_text="body"), full=True)
    fetch_mock = AsyncMock(return_value=full_msg)
    provider._fetch_message = fetch_mock  # type: ignore[method-assign]

    msg = await provider.read("m2")
    assert msg.id == "m2"
    assert msg.body == "body"


@pytest.mark.parametrize(
    "subject,to,body",
    [
        ("Hello", "bob@example.com", "Hi there"),
        ("Re: long subject with spaces", "x@y.z", "Multi\nline\nbody"),
    ],
)
async def test_send_encodes_mime_and_calls_api(subject: str, to: str, body: str) -> None:
    """send() builds a MIME message, base64-encodes it, and POSTs to Gmail."""
    platform = _make_platform("tok_send")
    provider = GmailProvider(platform=platform, tenant_id="local")  # type: ignore[arg-type]

    captured: dict[str, Any] = {}

    async def fake_send_request(path: str, *, json: dict[str, Any]) -> MagicMock:
        captured["raw"] = json["raw"]
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={"id": "sent_id"})
        return resp

    async def fake_post(url: str, **kwargs: Any) -> MagicMock:
        return await fake_send_request(url, **kwargs)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(side_effect=fake_post)
    provider._make_client = MagicMock(return_value=mock_client)  # type: ignore[method-assign]

    sent_id = await provider.send(to=to, subject=subject, body=body)
    assert sent_id == "sent_id"
    assert "raw" in captured
    # Verify the raw payload decodes back to valid RFC 2822 message containing the subject
    decoded = base64.urlsafe_b64decode(captured["raw"] + "==").decode("utf-8", errors="replace")
    assert to in decoded


@pytest.mark.parametrize(
    "unread,expected_body",
    [
        (False, {"removeLabelIds": ["UNREAD"]}),
        (True, {"addLabelIds": ["UNREAD"]}),
    ],
)
async def test_set_unread_modifies_unread_label(
    unread: bool, expected_body: dict[str, list[str]]
) -> None:
    """set_unread() POSTs a messages.modify that adds/removes the UNREAD label."""
    platform = _make_platform("tok_mark")
    provider = GmailProvider(platform=platform, tenant_id="local")  # type: ignore[arg-type]

    captured: dict[str, Any] = {}

    async def fake_post(url: str, **kwargs: Any) -> MagicMock:
        captured["url"] = url
        captured["json"] = kwargs["json"]
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        return resp

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(side_effect=fake_post)
    provider._make_client = MagicMock(return_value=mock_client)  # type: ignore[method-assign]

    await provider.set_unread("m42", unread=unread)
    assert captured["url"] == "/users/me/messages/m42/modify"
    assert captured["json"] == expected_body
    platform.get_oauth_token.assert_called_once_with("google")  # type: ignore[attr-defined]
