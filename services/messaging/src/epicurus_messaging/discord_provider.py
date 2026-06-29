"""The Discord bridge (#366) — a :class:`~epicurus_messaging.providers.BridgeProvider`.

**Inbound.** It maintains the Discord gateway (a WebSocket, via the maintained ``discord.py``
library) and, on each message, normalizes it to an :class:`~epicurus_core.InboundMessage`:
a guild text channel → ``channel_id``, a thread → ``channel_id`` (its parent) + ``thread_id``,
and the author → ``sender_*``. It **ignores its own messages** (so the agent's own replies
don't loop) and, in a busy server, only treats a message as a turn when the **bot is
@mentioned** — a direct message is always a turn. Reading message text needs the privileged
**Message Content Intent** (see the module docs for the bot setup).

**Outbound.** It consumes the reply via :meth:`send` and posts it with the REST API
(thread-aware: a reply to a thread goes to the thread). Discord caps a message at 2000
characters, so a long answer is split into chunks.

The bot token is the per-tenant secret ``messaging/discord`` in OpenBao, read through the
shared :func:`~epicurus_messaging.providers.load_bridge_secret`. With no token the bridge is
simply dormant (``configured=False``); the operator connects it from the web surface (#369),
which stores the token and triggers a reload so the gateway connects without a restart. This
module never calls an LLM (constraint #8) — it only speaks Discord's API.
"""

from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from epicurus_core import InboundMessage, OutboundMessage, SecretStore, get_logger
from epicurus_messaging.providers import BridgeStatus, InboundHandler, load_bridge_secret

if TYPE_CHECKING:  # imported lazily at runtime — keeps module import (and tests) network-free
    import discord

log = get_logger("messaging.discord")

DISCORD_BRIDGE = "discord"
# Discord rejects a message body over 2000 characters; a long agent reply is chunked.
DISCORD_MAX_CHARS = 2000

# A Discord user mention in message content: ``<@123>`` or the nickname form ``<@!123>``.
_MENTION_RE = re.compile(r"<@!?(\d+)>")


def chunk_text(text: str, limit: int = DISCORD_MAX_CHARS) -> list[str]:
    """Split ``text`` into ``<= limit``-character chunks for Discord's per-message cap.

    Empty/blank text yields no chunks (nothing to send). Splits on the character boundary —
    good enough for the foundation; smarter line/word-aware splitting is a later refinement.
    """
    text = text or ""
    if not text.strip():
        return []
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def strip_self_mentions(text: str, bot_user_id: int | None) -> str:
    """Remove the bot's own ``<@id>`` mentions so the agent sees a clean prompt."""
    if bot_user_id is None:
        return text.strip()
    cleaned = _MENTION_RE.sub(lambda m: "" if m.group(1) == str(bot_user_id) else m.group(0), text)
    return cleaned.strip()


def send_target_id(message: OutboundMessage) -> int:
    """The Discord channel id to post a reply to — the thread when there is one, else the channel.

    Discord models a thread as its own channel, so posting to ``thread_id`` keeps the reply in
    the thread; otherwise it goes to the parent ``channel_id``.
    """
    return int(message.thread_id or message.channel_id)


def to_inbound(
    message: Any, *, bot_user_id: int | None, tenant: str, bridge: str = DISCORD_BRIDGE
) -> InboundMessage | None:
    """Normalize a ``discord.Message`` to an :class:`InboundMessage`, or ``None`` to ignore it.

    Pure and duck-typed (no ``discord`` import) so it is unit-tested without the gateway.
    Returns ``None`` for the bot's own messages, other bots/webhooks, and — in a guild — any
    message that does not @mention the bot (a DM is always accepted). Thread messages map to
    the parent ``channel_id`` plus a ``thread_id``; a plain channel maps to ``channel_id`` only.
    """
    author = getattr(message, "author", None)
    author_id = getattr(author, "id", None)
    # Never answer ourselves (would loop), and skip other bots/webhooks.
    if bot_user_id is not None and author_id == bot_user_id:
        return None
    if getattr(author, "bot", False):
        return None

    is_dm = getattr(message, "guild", None) is None
    raw_mentions = getattr(message, "raw_mentions", []) or []
    mentioned = bot_user_id is not None and bot_user_id in raw_mentions
    # In a shared server only a direct @mention is a turn; a DM is always a turn.
    if not is_dm and not mentioned:
        return None

    channel = getattr(message, "channel", None)
    parent_id = getattr(channel, "parent_id", None)
    if parent_id:  # a thread: its own id is the thread, parent is the channel
        channel_id = str(parent_id)
        thread_id = str(getattr(channel, "id", ""))
    else:
        channel_id = str(getattr(channel, "id", ""))
        thread_id = None

    text = strip_self_mentions(getattr(message, "content", "") or "", bot_user_id)
    sender_name = getattr(author, "display_name", None) or getattr(author, "name", "") or ""
    return InboundMessage(
        tenant=tenant,
        bridge=bridge,
        channel_id=channel_id,
        thread_id=thread_id,
        sender_id=str(author_id) if author_id is not None else "",
        sender_name=sender_name,
        text=text,
        provider_msg_id=str(getattr(message, "id", "")),
    )


class DiscordProvider:
    """The Discord bridge backend (implements
    :class:`~epicurus_messaging.providers.BridgeProvider`)."""

    def __init__(self, *, secrets: SecretStore, tenant: str, bridge: str = DISCORD_BRIDGE) -> None:
        self._secrets = secrets
        self._tenant = tenant
        self._bridge = bridge
        self._on_inbound: InboundHandler | None = None
        self._client: discord.Client | None = None
        self._task: asyncio.Task[None] | None = None
        self._bot_user_id: int | None = None
        self._configured = False
        self._enabled = True
        self._connected = False
        self._detail = "not connected"

    def provider_name(self) -> str:
        return self._bridge

    def secret_names(self) -> list[str]:
        return [f"messaging/{self._bridge}"]

    async def start(self, on_inbound: InboundHandler) -> None:
        """Connect the gateway if a token is stored and the bridge is enabled; else stay dormant.

        Re-reads the stored token every call, so the manager reconnects a token change by
        calling :meth:`stop` then ``start`` again (ADR-0062). Never raises for the not-connected
        cases (no token / disabled) — they are normal states the status surface reports.
        """
        self._on_inbound = on_inbound
        try:
            token, enabled = await load_bridge_secret(
                self._secrets, self._bridge, tenant=self._tenant
            )
        except Exception as exc:  # vault unreachable → stay dormant, never abort the module
            self._configured = False
            self._connected = False
            self._detail = "token store unavailable"
            log.warning("discord bridge could not read its token", error=str(exc))
            return
        self._configured = token is not None
        self._enabled = enabled
        if token is None:
            self._connected = False
            self._detail = "no bot token set"
            return
        if not enabled:
            self._connected = False
            self._detail = "disabled by operator"
            return

        import discord  # lazy: keeps module import + unit tests free of the heavy dep

        intents = discord.Intents.default()
        intents.message_content = True  # privileged — required to read message text
        client = discord.Client(intents=intents)
        self._client = client
        self._detail = "connecting…"

        @client.event
        async def on_ready() -> None:
            self._connected = True
            self._bot_user_id = client.user.id if client.user else None
            self._detail = _summarize(client)
            log.info(
                "discord bridge connected",
                user=str(client.user),
                guilds=len(client.guilds),
            )

        @client.event
        async def on_message(message: discord.Message) -> None:
            inbound = to_inbound(
                message, bot_user_id=self._bot_user_id, tenant=self._tenant, bridge=self._bridge
            )
            if inbound is None or self._on_inbound is None:
                return
            await self._on_inbound(inbound)

        self._task = asyncio.create_task(self._run(token))

    async def _run(self, token: str) -> None:
        """Run the gateway until cancelled; record why it stopped for the status surface."""
        assert self._client is not None
        try:
            await self._client.start(token)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # invalid token, network, gateway close — surface, don't crash
            self._detail = f"gateway error: {type(exc).__name__}"
            log.error("discord gateway stopped", error=str(exc))
        finally:
            self._connected = False

    async def send(self, message: OutboundMessage) -> None:
        """Post the reply to its channel/thread; a no-op (logged) when not connected."""
        client = self._client
        if client is None or not self._connected:
            log.warning("discord bridge not connected; dropping reply", channel=message.channel_id)
            return
        chunks = chunk_text(message.text)
        if not chunks:
            return
        try:
            channel = client.get_partial_messageable(send_target_id(message))
            for chunk in chunks:
                await channel.send(chunk)
        except Exception as exc:  # one failed delivery must not break the subscription
            log.error("discord send failed", channel=message.channel_id, error=str(exc))

    async def stop(self) -> None:
        """Close the gateway and cancel its task (idempotent; safe before a reload)."""
        self._connected = False
        client, task = self._client, self._task
        self._client, self._task = None, None
        if client is not None:
            with suppress(Exception):
                await client.close()
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def status(self) -> BridgeStatus:
        return BridgeStatus(
            bridge=self._bridge,
            label="Discord",
            manageable=True,
            configured=self._configured,
            enabled=self._enabled,
            connected=self._connected,
            detail=self._detail,
        )


def _summarize(client: discord.Client) -> str:
    """A short human summary of the bot's reach for the status surface."""
    guilds = client.guilds
    channels = sum(len(g.text_channels) for g in guilds)
    return f"{len(guilds)} server(s) · {channels} channel(s)"
