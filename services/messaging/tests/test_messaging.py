"""Unit tests for the messaging module: manifest, provider selection, loopback, token helper.

No network — the NATS wiring is covered by test_messaging_app.py (integration)."""

from __future__ import annotations

from typing import Any

import pytest

from epicurus_core import (
    MESSAGING_INBOUND,
    MESSAGING_OUTBOUND,
    OutboundMessage,
    SecretError,
)
from epicurus_messaging.loopback_provider import LoopbackProvider
from epicurus_messaging.providers import BridgeProvider, bridge_token
from epicurus_messaging.service import build_module, build_provider
from epicurus_messaging.settings import MessagingSettings
from epicurus_messaging.telegram_provider import TelegramProvider


# ── manifest ────────────────────────────────────────────────────────────────────────────
async def test_module_manifest_declares_events_and_no_tools() -> None:
    manifest = await build_module(LoopbackProvider()).manifest()
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


async def test_manifest_secrets_follow_the_active_provider() -> None:
    # Loopback needs no credential, so the manifest declares no secrets.
    manifest = await build_module(LoopbackProvider()).manifest()
    assert manifest.secrets == []


async def test_manifest_secrets_follow_telegram_provider() -> None:
    # A real bridge's secret_names() flow into the manifest secrets[] for the operator to fill.
    provider = TelegramProvider(_FakeSecrets(), tenant="local")  # type: ignore[arg-type]
    manifest = await build_module(provider).manifest()
    assert manifest.secrets == ["messaging/telegram"]


# ── provider selection ──────────────────────────────────────────────────────────────────
def _settings(provider: str | None = None) -> MessagingSettings:
    kwargs: dict[str, Any] = {"service_name": "messaging"}
    if provider is not None:
        kwargs["messaging_provider"] = provider
    return MessagingSettings(**kwargs)


class _FakeSecrets:
    """Minimal SecretStore stand-in: maps path → data, else raises SecretError."""

    def __init__(self, data: dict[str, dict[str, Any]] | None = None) -> None:
        self._data = data or {}

    async def get(self, path: str, tenant_id: str | None = None) -> dict[str, Any]:
        if path not in self._data:
            raise SecretError(f"no secret at {path}")
        return self._data[path]


def test_build_provider_defaults_to_loopback() -> None:
    provider = build_provider(_settings("loopback"), _FakeSecrets())  # type: ignore[arg-type]
    assert isinstance(provider, LoopbackProvider)
    assert provider.provider_name() == "loopback"
    assert isinstance(provider, BridgeProvider)  # satisfies the seam (runtime_checkable)
    assert provider.connected() is True  # in-process echo — always live


def test_build_provider_selects_telegram() -> None:
    provider = build_provider(_settings("telegram"), _FakeSecrets())  # type: ignore[arg-type]
    assert isinstance(provider, TelegramProvider)
    assert provider.provider_name() == "telegram"
    assert provider.secret_names() == ["messaging/telegram"]
    assert isinstance(provider, BridgeProvider)  # satisfies the seam (runtime_checkable)
    assert provider.connected() is False  # not connected until start() loads a token


def test_build_provider_unknown_raises() -> None:
    with pytest.raises(ValueError, match="unknown messaging provider"):
        build_provider(_settings("signal"), _FakeSecrets())  # type: ignore[arg-type]


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


# ── bridge_token helper ─────────────────────────────────────────────────────────────────
async def test_bridge_token_reads_from_secret_store() -> None:
    secrets = _FakeSecrets({"messaging/telegram": {"token": "bot-123"}})
    token = await bridge_token(secrets, "telegram", tenant="local")  # type: ignore[arg-type]
    assert token == "bot-123"


async def test_bridge_token_absent_returns_none() -> None:
    token = await bridge_token(_FakeSecrets(), "telegram", tenant="local")  # type: ignore[arg-type]
    assert token is None


async def test_bridge_token_blank_returns_none() -> None:
    secrets = _FakeSecrets({"messaging/telegram": {"token": ""}})
    token = await bridge_token(secrets, "telegram", tenant="local")  # type: ignore[arg-type]
    assert token is None
