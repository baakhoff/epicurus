"""Messaging — a provider-pluggable chat-bridge module (ADR-0058)."""

from __future__ import annotations

from epicurus_messaging.loopback_provider import LoopbackProvider
from epicurus_messaging.providers import BridgeProvider, InboundHandler, bridge_token
from epicurus_messaging.service import MODULE_NAME, build_module, build_provider

__all__ = [
    "MODULE_NAME",
    "BridgeProvider",
    "InboundHandler",
    "LoopbackProvider",
    "bridge_token",
    "build_module",
    "build_provider",
]
