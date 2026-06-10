"""Provider-agnostic types for the LLM gateway."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel

Role = Literal["system", "user", "assistant", "tool"]


class ChatMessage(BaseModel):
    """One message in a chat exchange."""

    role: Role
    content: str


class ChatResult(BaseModel):
    """A non-streaming chat completion."""

    model: str
    content: str
    tool_calls: list[dict[str, Any]] | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class ModelInfo(BaseModel):
    """A model available in the local runtime."""

    name: str
    size: int | None = None


class ProviderInfo(BaseModel):
    """A configured LLM provider and whether its key is present."""

    alias: str
    local: bool
    configured: bool


class PowerState(StrEnum):
    """Runtime power state (ADR-0005)."""

    ACTIVE = "active"
    IDLE = "idle"
    PAUSED = "paused"
