"""Core-side messaging: the inbound-message consumer that turns an external
channel's message into a headless agent turn and routes the reply back out (ADR-0058)."""

from __future__ import annotations

from epicurus_core_app.messaging.inbound import InboundConsumer, TurnRunner

__all__ = ["InboundConsumer", "TurnRunner"]
