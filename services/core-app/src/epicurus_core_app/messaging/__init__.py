"""Core-side messaging: the inbound-message consumer that turns an external channel's message
into a headless agent turn and routes the reply back out (ADR-0058), plus the bridge-admin
surface the web shell uses to connect and manage bridges (ADR-0062, #369)."""

from __future__ import annotations

from epicurus_core_app.messaging.bridges import (
    BridgeAdmin,
    RegistryBridgeClient,
    create_messaging_router,
)
from epicurus_core_app.messaging.inbound import InboundConsumer, TurnRunner

__all__ = [
    "BridgeAdmin",
    "InboundConsumer",
    "RegistryBridgeClient",
    "TurnRunner",
    "create_messaging_router",
]
