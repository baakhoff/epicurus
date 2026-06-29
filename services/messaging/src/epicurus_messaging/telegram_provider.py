"""The Telegram bridge — a :class:`BridgeProvider` backed by the Bot API (#365).

The first real bridge on the messaging foundation (ADR-0058). Two directions:

* **Inbound** — a long poll over ``getUpdates`` normalizes each message onto an
  :class:`~epicurus_core.InboundMessage` (chat id → ``channel_id``, forum topic →
  ``thread_id``, ``from`` → ``sender_*``) and hands it to the module, which publishes
  ``messaging.inbound``;
* **Outbound** — ``sendMessage`` delivers the agent's reply to the same chat/thread,
  splitting on Telegram's 4096-character limit.

It reads its per-tenant bot token from OpenBao via
:func:`~epicurus_messaging.providers.bridge_token` (``messaging/telegram`` → ``token``) and never
calls an LLM (constraint #8). With **no token
stored** the bridge is simply idle — it logs and does not poll, mirroring a real bridge that is
"not connected"; set the token and restart to bring it up.

**Replies are sent as plain text** (no ``parse_mode``): an agent answer is arbitrary Markdown
that is *not* valid Telegram MarkdownV2 without escaping, and one malformed entity makes the API
reject the whole message — so plain text guarantees delivery. Rich formatting is a follow-up
(map the agent's Markdown to MarkdownV2 with proper escaping).

Long polling, not a webhook: a webhook needs a public HTTPS ingress, which the local-first
default deliberately does not expose (constraint #7); long polling reaches Telegram outbound-only.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import httpx

from epicurus_core import InboundMessage, OutboundMessage, SecretStore, get_logger
from epicurus_messaging.providers import InboundHandler, bridge_token

log = get_logger("messaging.telegram")

TELEGRAM_BRIDGE = "telegram"
# Telegram rejects a sendMessage whose text exceeds this; a long reply is split into chunks.
TELEGRAM_MAX_CHARS = 4096


class TelegramError(RuntimeError):
    """A Telegram Bot API call returned ``ok: false`` (the ``description`` is the message)."""


class TelegramProvider:
    """Telegram Bot API bridge. Implements :class:`~epicurus_messaging.providers.BridgeProvider`.

    Single-tenant for v1 (constructed with the module's default tenant), mirroring the
    foundation's single-tenant-by-subscription outbound consumer; multi-tenant fan-out is the
    same named follow-up (ADR-0058).
    """

    def __init__(
        self,
        secrets: SecretStore,
        *,
        tenant: str,
        api_base: str = "https://api.telegram.org",
        poll_timeout: int = 30,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._secrets = secrets
        self._tenant = tenant
        self._api_base = api_base.rstrip("/")
        self._poll_timeout = poll_timeout
        # An injected client (tests) is not owned — only a self-built one is closed on stop().
        self._client = client
        self._owns_client = client is None
        self._token: str | None = None
        self._on_inbound: InboundHandler | None = None
        self._task: asyncio.Task[None] | None = None
        self._offset: int | None = None  # next getUpdates offset (last update_id + 1)

    def provider_name(self) -> str:
        return TELEGRAM_BRIDGE

    def secret_names(self) -> list[str]:
        return [f"messaging/{TELEGRAM_BRIDGE}"]

    def connected(self) -> bool:
        """True once the bot token is loaded and the poll loop is running."""
        return self._token is not None and self._task is not None

    async def start(self, on_inbound: InboundHandler) -> None:
        """Load the token and begin long-polling; idle (no poll loop) if no token is stored."""
        self._on_inbound = on_inbound
        self._token = await bridge_token(self._secrets, TELEGRAM_BRIDGE, tenant=self._tenant)
        if self._token is None:
            log.warning(
                "telegram bridge has no token; idle until messaging/telegram is set and the "
                "service restarts",
                tenant=self._tenant,
            )
            return
        if self._client is None:
            # The read timeout must outlast a long poll, or the client aborts mid-wait.
            timeout = httpx.Timeout(self._poll_timeout + 10.0)
            self._client = httpx.AsyncClient(base_url=self._api_base, timeout=timeout)
        self._task = asyncio.create_task(self._poll_loop())
        log.info("telegram bridge started", tenant=self._tenant, poll_timeout=self._poll_timeout)

    async def send(self, message: OutboundMessage) -> None:
        """Deliver a reply via ``sendMessage``, split to Telegram's length limit, thread-aware."""
        if not message.text.strip():
            return  # nothing to say — Telegram rejects an empty message anyway
        token, client = self._token, self._client
        if token is None or client is None:
            log.warning("telegram send skipped; bridge not connected", channel=message.channel_id)
            return
        for index, chunk in enumerate(_split_text(message.text)):
            payload: dict[str, Any] = {"chat_id": message.channel_id, "text": chunk}
            if message.thread_id:
                payload["message_thread_id"] = _as_int(message.thread_id)
            # Quote the user's message on the first chunk only.
            if index == 0 and message.reply_to_msg_id:
                payload["reply_parameters"] = {"message_id": _as_int(message.reply_to_msg_id)}
            try:
                await self._call(client, token, "sendMessage", payload)
            except (httpx.HTTPError, TelegramError) as exc:
                # Contain a delivery failure (bad chat, auth, rate limit) so it never breaks the
                # outbound subscription; stop on the first error rather than spamming the rest.
                log.warning(
                    "telegram sendMessage failed", channel=message.channel_id, error=str(exc)
                )
                return

    async def stop(self) -> None:
        """Stop polling and release the client (only if we built it)."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── internals ───────────────────────────────────────────────────────────────────────
    async def _poll_loop(self) -> None:
        """Long-poll ``getUpdates`` forever, publishing each message; back off on failure."""
        assert self._client is not None and self._token is not None
        backoff = 1.0
        while True:
            try:
                updates = await self._get_updates(self._client, self._token)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network blip, JSON, or API error — retry with backoff.
                log.warning("telegram getUpdates failed; backing off", error=str(exc))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)
                continue
            backoff = 1.0
            for update in updates:
                try:
                    self._offset = int(update["update_id"]) + 1
                    inbound = self._to_inbound(update)
                    if inbound is not None and self._on_inbound is not None:
                        await self._on_inbound(inbound)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # one bad update must not stop the loop (ADR-0058).
                    log.warning("dropping telegram update", error=str(exc))

    async def _get_updates(self, client: httpx.AsyncClient, token: str) -> list[dict[str, Any]]:
        """One ``getUpdates`` long poll; ``allowed_updates`` is narrowed to plain messages."""
        params: dict[str, Any] = {"timeout": self._poll_timeout, "allowed_updates": ["message"]}
        if self._offset is not None:
            params["offset"] = self._offset
        result = await self._call(client, token, "getUpdates", params)
        return result if isinstance(result, list) else []

    def _to_inbound(self, update: dict[str, Any]) -> InboundMessage | None:
        """Map a Telegram update onto an :class:`InboundMessage`, or ``None`` to skip it.

        v1 handles text messages; media without a caption, stickers, edits, and service
        messages have no usable text and are ignored (their bytes are a follow-up, ADR-0058).
        """
        message = update.get("message")
        if not isinstance(message, dict):
            return None  # not a new message (edited_message, callback_query, …)
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = message.get("text") or message.get("caption") or ""
        if chat_id is None or not text:
            return None
        sender = message.get("from") or {}
        thread_id: str | None = None
        if message.get("is_topic_message") and message.get("message_thread_id") is not None:
            thread_id = str(message["message_thread_id"])
        sender_id = sender.get("id")
        message_id = message.get("message_id")
        return InboundMessage(
            tenant=self._tenant,
            bridge=TELEGRAM_BRIDGE,
            channel_id=str(chat_id),
            thread_id=thread_id,
            sender_id="" if sender_id is None else str(sender_id),
            sender_name=_display_name(sender),
            text=text,
            provider_msg_id="" if message_id is None else str(message_id),
        )

    async def _call(
        self, client: httpx.AsyncClient, token: str, method: str, payload: dict[str, Any]
    ) -> Any:
        """POST a Bot API method and unwrap the ``{ok, result}`` envelope (raises on failure)."""
        resp = await client.post(f"/bot{token}/{method}", json=payload)
        resp.raise_for_status()
        body = resp.json()
        if not body.get("ok", False):
            raise TelegramError(str(body.get("description", "telegram API error")))
        return body.get("result")


def _display_name(sender: dict[str, Any]) -> str:
    """A human label for the author: ``first last``, else ``@username``, else empty."""
    name = " ".join(part for part in (sender.get("first_name"), sender.get("last_name")) if part)
    if name:
        return name
    username = sender.get("username")
    return str(username) if username else ""


def _split_text(text: str, limit: int = TELEGRAM_MAX_CHARS) -> list[str]:
    """Split ``text`` into ``<= limit``-char chunks, preferring a newline/space boundary."""
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n")
        if cut <= 0:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit  # no boundary in range — hard-cut at the limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
        if cut < limit:  # broke on whitespace — drop the boundary char joining the chunks
            remaining = remaining[1:]
    if remaining:
        chunks.append(remaining)
    return chunks


def _as_int(value: str) -> int | str:
    """Telegram wants integer ids; fall back to the raw value if it is not numeric."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return value
