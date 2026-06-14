"""Provider-agnostic types for the LLM gateway.

The chat shapes (``ChatMessage`` / ``ChatResult`` / ``Role``) are the shared chat
contract — re-exported here from ``epicurus_core`` (ADR-0021) so the gateway,
agent, and routes keep importing them from one place. The remaining types are
gateway-internal.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from epicurus_core import ChatMessage, ChatResult, Role

__all__ = [
    "ChatMessage",
    "ChatResult",
    "ModelInfo",
    "PowerState",
    "ProviderInfo",
    "Role",
    "StreamEvent",
    "UsageEvent",
]


class StreamEvent(BaseModel):
    """One increment of a streaming completion.

    ``delta`` events carry a content token; the final event carries the assembled
    ``result`` (full content plus any tool calls accumulated from the stream).
    """

    delta: str | None = None
    result: ChatResult | None = None


class ModelInfo(BaseModel):
    """A model available in the local runtime."""

    name: str
    size: int | None = None
    # Currently held in memory by the runtime (drives the UI's "loaded" hint).
    loaded: bool = False


class ProviderInfo(BaseModel):
    """A configured LLM provider and whether its key is present."""

    alias: str
    local: bool
    configured: bool
    # The "custom" (any-OpenAI-compatible) provider also needs an endpoint URL.
    needs_base_url: bool = False


class UsageEvent(BaseModel):
    """Emitted on NATS (``<tenant>.llm.usage``) after each call — no content, no keys."""

    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int
    tenant: str


class PowerState(StrEnum):
    """Runtime power state (ADR-0005)."""

    ACTIVE = "active"
    IDLE = "idle"
    PAUSED = "paused"
