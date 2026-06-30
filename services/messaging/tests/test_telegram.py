"""Unit tests for the Telegram bridge (#365): update mapping, length splitting, send, poll.

No real network — the Bot API is faked with ``httpx.MockTransport`` injected into the
provider, and OpenBao with a tiny in-memory ``SecretStore`` stand-in.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from epicurus_core import OutboundMessage, SecretError
from epicurus_messaging.telegram_provider import (
    TelegramProvider,
    _display_name,
    _split_text,
)


class _FakeSecrets:
    """Minimal SecretStore stand-in: maps path → data, else raises SecretError."""

    def __init__(self, data: dict[str, dict[str, Any]] | None = None) -> None:
        self._data = data or {}

    async def get(self, path: str, tenant_id: str | None = None) -> dict[str, Any]:
        if path not in self._data:
            raise SecretError(f"no secret at {path}")
        return self._data[path]


def _with_token() -> _FakeSecrets:
    return _FakeSecrets({"messaging/telegram": {"token": "bot-123"}})


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url="http://tg.test", transport=httpx.MockTransport(handler))


def _provider(secrets: _FakeSecrets, **kwargs: Any) -> TelegramProvider:
    return TelegramProvider(secrets, tenant="local", **kwargs)  # type: ignore[arg-type]


# ── update → InboundMessage mapping ─────────────────────────────────────────────────────
def test_to_inbound_maps_a_plain_message() -> None:
    provider = _provider(_FakeSecrets())
    update = {
        "update_id": 1,
        "message": {
            "message_id": 55,
            "chat": {"id": 4242, "type": "private"},
            "from": {"id": 7, "first_name": "Ada", "last_name": "Lovelace"},
            "text": "what is on my calendar?",
        },
    }
    inbound = provider._to_inbound(update)
    assert inbound is not None
    assert inbound.bridge == "telegram"
    assert inbound.tenant == "local"
    assert inbound.channel_id == "4242"
    assert inbound.thread_id is None
    assert inbound.sender_id == "7"
    assert inbound.sender_name == "Ada Lovelace"
    assert inbound.text == "what is on my calendar?"
    assert inbound.provider_msg_id == "55"
    assert inbound.session_id() == "telegram:4242"


def test_to_inbound_maps_a_forum_topic_to_thread() -> None:
    provider = _provider(_FakeSecrets())
    update = {
        "update_id": 2,
        "message": {
            "message_id": 9,
            "chat": {"id": -100123, "type": "supergroup"},
            "from": {"id": 7, "first_name": "Ada"},
            "is_topic_message": True,
            "message_thread_id": 88,
            "text": "hi",
        },
    }
    inbound = provider._to_inbound(update)
    assert inbound is not None
    assert inbound.channel_id == "-100123"
    assert inbound.thread_id == "88"
    assert inbound.session_id() == "telegram:-100123:88"


def test_to_inbound_uses_username_when_no_name() -> None:
    provider = _provider(_FakeSecrets())
    update = {
        "update_id": 3,
        "message": {
            "message_id": 1,
            "chat": {"id": 1, "type": "private"},
            "from": {"id": 7, "username": "ada"},
            "text": "hi",
        },
    }
    inbound = provider._to_inbound(update)
    assert inbound is not None
    assert inbound.sender_name == "ada"


def test_to_inbound_uses_caption_as_text() -> None:
    provider = _provider(_FakeSecrets())
    update = {
        "update_id": 4,
        "message": {
            "message_id": 1,
            "chat": {"id": 1, "type": "private"},
            "from": {"id": 7, "first_name": "Ada"},
            "caption": "a photo caption",
            "photo": [{"file_id": "abc"}],
        },
    }
    inbound = provider._to_inbound(update)
    assert inbound is not None
    assert inbound.text == "a photo caption"


def test_to_inbound_skips_non_message_updates() -> None:
    provider = _provider(_FakeSecrets())
    # An edited message / callback query has no "message" key — ignored for v1.
    assert provider._to_inbound({"update_id": 5, "edited_message": {"text": "x"}}) is None
    assert provider._to_inbound({"update_id": 6}) is None


def test_to_inbound_skips_messages_without_text() -> None:
    provider = _provider(_FakeSecrets())
    # A sticker / service message has no usable text — skipped (no empty turn).
    update = {
        "update_id": 7,
        "message": {"message_id": 1, "chat": {"id": 1}, "from": {"id": 7}, "sticker": {}},
    }
    assert provider._to_inbound(update) is None


# ── display name + length splitting (pure helpers) ──────────────────────────────────────
def test_display_name_prefers_full_name() -> None:
    assert _display_name({"first_name": "Ada", "last_name": "Lovelace"}) == "Ada Lovelace"
    assert _display_name({"first_name": "Ada"}) == "Ada"
    assert _display_name({"username": "ada"}) == "ada"
    assert _display_name({}) == ""


def test_split_text_keeps_short_text_whole() -> None:
    assert _split_text("hello") == ["hello"]


def test_split_text_breaks_on_newline_boundary() -> None:
    text = "a" * 4000 + "\n" + "b" * 4000
    chunks = _split_text(text)
    assert len(chunks) == 2
    assert chunks[0] == "a" * 4000  # broke at the newline, which is dropped
    assert chunks[1] == "b" * 4000
    assert all(len(c) <= 4096 for c in chunks)


def test_split_text_hard_cuts_when_no_boundary() -> None:
    text = "x" * 5000  # no whitespace to break on
    chunks = _split_text(text)
    assert len(chunks) == 2
    assert len(chunks[0]) == 4096
    assert len(chunks[1]) == 904
    assert "".join(chunks) == text  # nothing dropped on a hard cut


# ── send ────────────────────────────────────────────────────────────────────────────────
async def test_send_splits_threads_and_quotes_first_chunk_only() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/botbot-123/sendMessage"
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {}})

    provider = _provider(_with_token(), client=_client(handler))
    provider._token = "bot-123"  # send() needs a loaded token; skip the poll loop here

    await provider.send(
        OutboundMessage(
            tenant="local",
            bridge="telegram",
            channel_id="4242",
            thread_id="88",
            text="y" * 5000,
            reply_to_msg_id="55",
        )
    )

    assert len(sent) == 2  # 5000 > 4096 → two chunks
    assert all(r["chat_id"] == "4242" for r in sent)
    assert all(r["message_thread_id"] == 88 for r in sent)  # thread on every chunk (int)
    assert sent[0]["reply_parameters"] == {"message_id": 55}  # quote on the first …
    assert "reply_parameters" not in sent[1]  # … not on the rest
    assert sum(len(r["text"]) for r in sent) == 5000


async def test_send_without_token_is_a_noop() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True, "result": {}})

    provider = _provider(_FakeSecrets(), client=_client(handler))  # never started → no token
    await provider.send(
        OutboundMessage(tenant="local", bridge="telegram", channel_id="1", text="hi")
    )
    assert calls == 0


async def test_send_empty_text_is_a_noop() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True, "result": {}})

    provider = _provider(_with_token(), client=_client(handler))
    provider._token = "bot-123"
    await provider.send(
        OutboundMessage(tenant="local", bridge="telegram", channel_id="1", text="   ")
    )
    assert calls == 0


# ── start / poll loop / connected ───────────────────────────────────────────────────────
async def test_start_without_token_idles() -> None:
    provider = _provider(_FakeSecrets())  # no token stored

    async def _on(_: Any) -> None:  # pragma: no cover - never called
        raise AssertionError("idle bridge must not receive")

    await provider.start(_on)
    assert (await provider.status()).connected is False
    await provider.stop()  # clean even though nothing started


async def test_poll_loop_publishes_inbound_and_advances_offset() -> None:
    seen: list[Any] = []
    got = asyncio.Event()
    offsets: list[Any] = []
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates"):
            body = json.loads(request.content)
            offsets.append(body.get("offset"))
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": [
                            {
                                "update_id": 10,
                                "message": {
                                    "message_id": 5,
                                    "chat": {"id": 4242, "type": "private"},
                                    "from": {"id": 7, "first_name": "Ada"},
                                    "text": "hi",
                                },
                            }
                        ],
                    },
                )
            # After the first batch, fail the poll so the loop hits its backoff `sleep` — a real
            # suspension that yields control back to the test. An instant mock that always
            # returned `[]` would let the `while True` loop spin without ever yielding (event-loop
            # starvation); stop() cancels the backoff sleep immediately.
            return httpx.Response(200, json={"ok": False, "description": "halt"})
        return httpx.Response(200, json={"ok": True, "result": {}})

    provider = _provider(_with_token(), client=_client(handler), poll_timeout=0)

    async def _on(msg: Any) -> None:
        seen.append(msg)
        got.set()

    await provider.start(_on)
    assert (await provider.status()).connected is True
    try:
        await asyncio.wait_for(got.wait(), timeout=5)
    finally:
        await provider.stop()

    assert len(seen) == 1
    assert seen[0].channel_id == "4242"
    assert seen[0].text == "hi"
    assert provider._offset == 11  # last update_id (10) + 1
    assert offsets[0] is None  # the first poll has no offset …
