"""Provider-agnostic types for the LLM gateway."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel

Role = Literal["system", "user", "assistant", "tool"]


class ChatMessage(BaseModel):
    """One message in a chat exchange.

    ``content`` is optional: an assistant tool-call turn carries ``tool_calls`` with no
    content, and a ``tool`` result carries ``tool_call_id`` + ``name``.
    """

    role: Role
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    name: str | None = None


class ChatResult(BaseModel):
    """A non-streaming chat completion."""

    model: str
    content: str
    tool_calls: list[dict[str, Any]] | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


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
