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
    "ModelDetails",
    "ModelInfo",
    "PowerState",
    "ProviderInfo",
    "Role",
    "StreamEvent",
    "UsageEvent",
]


class StreamEvent(BaseModel):
    """One increment of a streaming completion.

    ``delta`` events carry a content token; ``reasoning`` events carry a chain-of-thought
    token (kept separate so the UI shows thinking without polluting the answer, ADR-0041);
    the final event carries the assembled ``result`` (full content, reasoning, and any tool
    calls accumulated from the stream).
    """

    delta: str | None = None
    reasoning: str | None = None
    result: ChatResult | None = None


class ModelInfo(BaseModel):
    """A model available in the local runtime."""

    name: str
    size: int | None = None
    # Currently held in memory by the runtime (drives the UI's "loaded" hint).
    loaded: bool = False
    # Hidden from chat pickers; still visible in the model manager so it can be toggled back.
    hidden: bool = False
    # What the runtime reports the model can do (e.g. "tools", "vision", "embedding"), from
    # /api/show. Only populated when explicitly requested (it costs one /api/show per model);
    # empty otherwise — and an empty list also means "the runtime reported none/unknown".
    capabilities: list[str] = []
    # The model's trained maximum context (#618). Same opt-in as `capabilities` — `None` means
    # not requested or not reported, never a fake default.
    context_length: int | None = None


class ModelDetails(BaseModel):
    """Read-only facts about a local model, from the runtime's ``/api/show``.

    Surfaced in the model-settings sheet. Weight ``quantization`` is fixed when the model is
    pulled (e.g. ``Q4_K_M``) — to change it the operator pulls a different variant; it is
    *not* a runtime knob. ``context_length`` is the model's trained maximum (a ceiling for the
    operator's per-model context-window choice). ``capabilities`` is what the runtime says the
    model can do (e.g. ``tools``, ``vision``) — drives tool gating + the chat capability hint.
    Any field is ``None``/empty when the runtime did not report it (or the model isn't local)."""

    quantization: str | None = None
    parameter_size: str | None = None
    context_length: int | None = None
    family: str | None = None
    capabilities: list[str] = []


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
    # Set only when an automation run made the call (ADR-0105): the second half of the dual
    # attribution the SaaS overlay meters on. ``tenant`` answers "who is billed"; this
    # answers "which of their automations spent it" — without it, an automation quietly
    # burning tokens is indistinguishable from the operator's own chatting. Additive and
    # optional, so an existing consumer is unaffected and an ordinary turn omits it.
    automation_id: str | None = None


class PowerState(StrEnum):
    """Runtime power state (ADR-0005)."""

    ACTIVE = "active"
    IDLE = "idle"
    PAUSED = "paused"
