"""Unit tests for the messaging module: manifest, bridge manager, loopback, Discord mapping,
secret loader. No network — the NATS wiring is covered by test_messaging_app.py (integration)
and the Discord gateway itself is not exercised (the pure mapping helpers are)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from epicurus_core import (
    MESSAGING_INBOUND,
    MESSAGING_OUTBOUND,
    OutboundMessage,
    SecretError,
)
from epicurus_messaging.discord_provider import (
    DiscordProvider,
    chunk_text,
    send_target_id,
    strip_self_mentions,
    to_inbound,
)
from epicurus_messaging.loopback_provider import LoopbackProvider
from epicurus_messaging.manager import BridgeManager
from epicurus_messaging.providers import (
    BridgeProvider,
    BridgeStatus,
    InboundHandler,
    bridge_token,
    load_bridge_secret,
)
from epicurus_messaging.service import build_bridges, build_module
from epicurus_messaging.settings import MessagingSettings


def _settings() -> MessagingSettings:
    return MessagingSettings(service_name="messaging")


class _FakeSecrets:
    """Minimal SecretStore stand-in: maps path → data, else raises SecretError."""

    def __init__(self, data: dict[str, dict[str, Any]] | None = None) -> None:
        self._data = data or {}

    async def get(self, path: str, tenant_id: str | None = None) -> dict[str, Any]:
        if path not in self._data:
            raise SecretError(f"no secret at {path}")
        return self._data[path]


# ── manifest ────────────────────────────────────────────────────────────────────────────
async def test_module_manifest_declares_events_no_tools_and_bridge_secrets() -> None:
    manager = build_bridges(_settings(), _FakeSecrets())  # type: ignore[arg-type]
    manifest = await build_module(manager).manifest()
    assert manifest.name == "messaging"
    assert manifest.version == "0.2.0"
    # It is a transport bridge, not an agent capability — it exposes no MCP tools.
    assert manifest.tools == []
    emitted = {e.subject for e in manifest.events_emitted}
    consumed = {e.subject for e in manifest.events_consumed}
    assert MESSAGING_INBOUND in emitted
    assert MESSAGING_OUTBOUND in consumed
    assert manifest.ui is not None
    assert manifest.ui.status_url == "/status"
    # The real bridges' secret paths flow into the manifest (loopback contributes none).
    assert "messaging/discord" in manifest.secrets


# ── bridge manager ──────────────────────────────────────────────────────────────────────
def test_build_bridges_runs_loopback_discord_and_telegram() -> None:
    manager = build_bridges(_settings(), _FakeSecrets())  # type: ignore[arg-type]
    names = manager.provider_names()
    assert "loopback" in names
    assert "discord" in names
    assert "telegram" in names
    assert manager.secret_names() == ["messaging/discord", "messaging/telegram"]
    assert isinstance(manager.loopback, LoopbackProvider)


class _RecordingProvider:
    """A fake bridge that records start/stop/send for manager routing tests."""

    def __init__(self, name: str) -> None:
        self._name = name
        self.started = 0
        self.stopped = 0
        self.sent: list[OutboundMessage] = []

    def provider_name(self) -> str:
        return self._name

    def secret_names(self) -> list[str]:
        return []

    async def start(self, on_inbound: InboundHandler) -> None:
        self.started += 1

    async def send(self, message: OutboundMessage) -> None:
        self.sent.append(message)

    async def stop(self) -> None:
        self.stopped += 1

    async def status(self) -> BridgeStatus:
        return BridgeStatus(bridge=self._name, label=self._name, connected=self.started > 0)


def _manager(*providers: _RecordingProvider) -> BridgeManager:
    loopback = LoopbackProvider()
    return BridgeManager([loopback, *providers], loopback=loopback)


async def test_manager_dispatches_outbound_by_bridge() -> None:
    a, b = _RecordingProvider("discord"), _RecordingProvider("telegram")
    manager = _manager(a, b)

    async def _noop(_msg: Any) -> None: ...

    await manager.start_all(_noop)
    await manager.dispatch(OutboundMessage(tenant="t", bridge="telegram", channel_id="c", text="x"))
    assert [m.text for m in b.sent] == ["x"]
    assert a.sent == []  # only the addressed bridge receives it


async def test_manager_drops_outbound_for_unknown_bridge() -> None:
    manager = _manager(_RecordingProvider("discord"))

    async def _noop(_msg: Any) -> None: ...

    await manager.start_all(_noop)
    # No raise, no delivery — an unknown bridge is logged and dropped.
    await manager.dispatch(OutboundMessage(tenant="t", bridge="slack", channel_id="c", text="x"))


async def test_manager_reload_restarts_one_bridge() -> None:
    target = _RecordingProvider("discord")
    manager = _manager(target)

    async def _noop(_msg: Any) -> None: ...

    await manager.start_all(_noop)
    assert target.started == 1
    status = await manager.reload("discord")
    assert target.stopped == 1
    assert target.started == 2  # stop, then start again
    assert status.bridge == "discord"


async def test_manager_reload_unknown_raises_keyerror() -> None:
    manager = _manager()

    async def _noop(_msg: Any) -> None: ...

    await manager.start_all(_noop)
    with pytest.raises(KeyError):
        await manager.reload("nope")


async def test_manager_reload_before_start_raises_runtimeerror() -> None:
    with pytest.raises(RuntimeError, match="not started"):
        await _manager(_RecordingProvider("discord")).reload("discord")


async def test_manager_status_lists_every_bridge() -> None:
    manager = _manager(_RecordingProvider("discord"))

    async def _noop(_msg: Any) -> None: ...

    await manager.start_all(_noop)
    body = await manager.status()
    assert body["inbound_subject"] == MESSAGING_INBOUND
    assert body["outbound_subject"] == MESSAGING_OUTBOUND
    bridges = {b["bridge"] for b in body["bridges"]}  # type: ignore[index,union-attr]
    assert {"loopback", "discord"} <= bridges


# ── loopback provider ───────────────────────────────────────────────────────────────────
async def test_loopback_inject_calls_handler() -> None:
    provider = LoopbackProvider()
    received: list[Any] = []

    async def _on(msg: Any) -> None:
        received.append(msg)

    await provider.start(_on)
    msg = await provider.inject(tenant="local", channel_id="c1", text="hi", thread_id="t1")
    assert received == [msg]
    assert msg.bridge == "loopback"
    assert msg.session_id() == "loopback:c1:t1"


async def test_loopback_inject_before_start_raises() -> None:
    with pytest.raises(RuntimeError, match="not started"):
        await LoopbackProvider().inject(tenant="local", channel_id="c1", text="hi")


async def test_loopback_send_records_reply_without_relooping() -> None:
    provider = LoopbackProvider()
    relooped: list[Any] = []

    async def _on(msg: Any) -> None:
        relooped.append(msg)

    await provider.start(_on)
    await provider.send(
        OutboundMessage(tenant="local", bridge="loopback", channel_id="c1", text="ok")
    )
    assert len(provider.sent) == 1
    assert provider.sent[0].text == "ok"
    assert relooped == []  # a reply must never become a new inbound (would loop forever)


async def test_loopback_status_is_unmanageable_and_on() -> None:
    provider = LoopbackProvider()
    status = await provider.status()
    assert status.manageable is False
    assert status.connected is True
    assert isinstance(provider, BridgeProvider)  # satisfies the seam (runtime_checkable)


# ── Discord mapping helpers (no gateway) ─────────────────────────────────────────────────
def _msg(
    *,
    author_id: int = 1,
    bot: bool = False,
    content: str = "hi",
    channel_id: int = 100,
    parent_id: int | None = None,
    guild: object | None = object(),
    mentions: list[int] | None = None,
    msg_id: int = 999,
) -> SimpleNamespace:
    channel = SimpleNamespace(id=channel_id, parent_id=parent_id)
    author = SimpleNamespace(id=author_id, bot=bot, display_name="Alice", name="alice")
    return SimpleNamespace(
        author=author,
        channel=channel,
        content=content,
        guild=guild,
        raw_mentions=mentions or [],
        id=msg_id,
    )


def test_to_inbound_ignores_own_messages() -> None:
    assert to_inbound(_msg(author_id=42), bot_user_id=42, tenant="t") is None


def test_to_inbound_ignores_other_bots() -> None:
    assert to_inbound(_msg(bot=True), bot_user_id=42, tenant="t") is None


def test_to_inbound_dm_is_always_a_turn() -> None:
    inbound = to_inbound(_msg(guild=None, content="hello"), bot_user_id=42, tenant="t")
    assert inbound is not None
    assert inbound.bridge == "discord"
    assert inbound.text == "hello"
    assert inbound.thread_id is None


def test_to_inbound_guild_requires_a_mention() -> None:
    # In a server, an un-mentioned message is ignored…
    assert to_inbound(_msg(mentions=[]), bot_user_id=42, tenant="t") is None
    # …but a message mentioning the bot is a turn, with the mention stripped from the text.
    inbound = to_inbound(_msg(content="<@42> what's up", mentions=[42]), bot_user_id=42, tenant="t")
    assert inbound is not None
    assert inbound.text == "what's up"


def test_to_inbound_thread_maps_parent_and_thread() -> None:
    inbound = to_inbound(
        _msg(channel_id=555, parent_id=100, guild=None), bot_user_id=42, tenant="t"
    )
    assert inbound is not None
    assert inbound.channel_id == "100"  # the parent channel
    assert inbound.thread_id == "555"  # the thread's own id


def test_strip_self_mentions_only_strips_the_bot() -> None:
    assert strip_self_mentions("<@42> hi <@7>", 42) == "hi <@7>"
    assert strip_self_mentions("<@!42> yo", 42) == "yo"
    assert strip_self_mentions("plain", None) == "plain"


def test_send_target_id_prefers_thread() -> None:
    assert send_target_id(OutboundMessage(tenant="t", bridge="discord", channel_id="100")) == 100
    msg = OutboundMessage(tenant="t", bridge="discord", channel_id="100", thread_id="555")
    assert send_target_id(msg) == 555


def test_chunk_text_splits_on_the_limit() -> None:
    assert chunk_text("") == []
    assert chunk_text("   ") == []
    assert chunk_text("abc", limit=10) == ["abc"]
    assert chunk_text("abcdef", limit=2) == ["ab", "cd", "ef"]
    big = "x" * 4500
    chunks = chunk_text(big, limit=2000)
    assert [len(c) for c in chunks] == [2000, 2000, 500]


# ── Discord provider dormant states (no gateway) ─────────────────────────────────────────
async def test_discord_dormant_without_token() -> None:
    provider = DiscordProvider(secrets=_FakeSecrets(), tenant="local")  # type: ignore[arg-type]

    async def _noop(_msg: Any) -> None: ...

    await provider.start(_noop)
    status = await provider.status()
    assert status.bridge == "discord"
    assert status.manageable is True
    assert status.configured is False
    assert status.connected is False
    assert "no bot token" in status.detail


async def test_discord_dormant_when_disabled() -> None:
    secrets = _FakeSecrets({"messaging/discord": {"token": "tok", "enabled": False}})
    provider = DiscordProvider(secrets=secrets, tenant="local")  # type: ignore[arg-type]

    async def _noop(_msg: Any) -> None: ...

    await provider.start(_noop)
    status = await provider.status()
    assert status.configured is True  # a token is stored…
    assert status.enabled is False  # …but the operator turned it off
    assert status.connected is False


async def test_discord_send_when_not_connected_is_a_noop() -> None:
    provider = DiscordProvider(secrets=_FakeSecrets(), tenant="local")  # type: ignore[arg-type]
    # Never connected → no client; send must not raise.
    await provider.send(OutboundMessage(tenant="t", bridge="discord", channel_id="100", text="x"))


# ── secret loader ───────────────────────────────────────────────────────────────────────
async def test_load_bridge_secret_reads_token_and_enabled() -> None:
    secrets = _FakeSecrets({"messaging/discord": {"token": "tok", "enabled": False}})
    token, enabled = await load_bridge_secret(secrets, "discord", tenant="local")  # type: ignore[arg-type]
    assert token == "tok"
    assert enabled is False


async def test_load_bridge_secret_defaults_enabled_true() -> None:
    secrets = _FakeSecrets({"messaging/discord": {"token": "tok"}})
    token, enabled = await load_bridge_secret(secrets, "discord", tenant="local")  # type: ignore[arg-type]
    assert token == "tok"
    assert enabled is True


async def test_load_bridge_secret_absent_returns_none_true() -> None:
    token, enabled = await load_bridge_secret(_FakeSecrets(), "discord", tenant="local")  # type: ignore[arg-type]
    assert token is None
    assert enabled is True


async def test_bridge_token_reads_token_only() -> None:
    secrets = _FakeSecrets({"messaging/telegram": {"token": "bot-123"}})
    assert await bridge_token(secrets, "telegram", tenant="local") == "bot-123"  # type: ignore[arg-type]
    assert await bridge_token(_FakeSecrets(), "telegram", tenant="local") is None  # type: ignore[arg-type]
    blank = _FakeSecrets({"messaging/telegram": {"token": ""}})
    assert await bridge_token(blank, "telegram", tenant="local") is None  # type: ignore[arg-type]
