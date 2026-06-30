"""Messaging — a provider-pluggable chat-bridge module (ADR-0058 / ADR-0062)."""

from __future__ import annotations

from epicurus_messaging.discord_provider import DiscordProvider
from epicurus_messaging.loopback_provider import LoopbackProvider
from epicurus_messaging.manager import BridgeManager
from epicurus_messaging.providers import (
    BridgeProvider,
    BridgeStatus,
    InboundHandler,
    bridge_token,
    load_bridge_secret,
)
from epicurus_messaging.service import MODULE_NAME, build_bridges, build_module

__all__ = [
    "MODULE_NAME",
    "BridgeManager",
    "BridgeProvider",
    "BridgeStatus",
    "DiscordProvider",
    "InboundHandler",
    "LoopbackProvider",
    "bridge_token",
    "build_bridges",
    "build_module",
    "load_bridge_secret",
]
