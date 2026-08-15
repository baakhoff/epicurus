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
    "ToolCallFragment",
    "UsageEvent",
]


class ToolCallFragment(BaseModel):
    """One increment of a tool call, as the provider streams it (#654).

    The gateway has always assembled tool-call fragments internally and surfaced them only on
    the final ``result``; a consumer that wants to watch a call *being written* — the document
    pane's typewriter (ADR-0121) — needs them as they arrive. This is that view, and it is
    strictly additive: the accumulation and the final ``result`` are unchanged, so a consumer
    that ignores ``StreamEvent.tool_call`` behaves exactly as before.

    ``slot`` is the gateway's own accumulator slot — the *same* number the assembly used, not a
    second guess at it, so the hard-won index/slot discipline (#324: OpenAI shares an ``index``
    across a call's fragments, LiteLLM leaves it unset for Ollama's complete-per-fragment calls)
    is honoured here for free. Fragments of one call always carry one ``slot``, and two calls
    never share one within a stream.

    ``id`` and ``name`` are the call's values **as known so far** (the accumulator resolves each
    once, so a continuation fragment that carried neither still reports both) — a consumer can
    therefore act on the tool's identity from the first fragment that names it, without tracking
    state of its own. ``arguments`` is the opposite: strictly the *delta* this fragment added to
    the arguments JSON, never the accumulation. It is ``None`` when the fragment added no
    argument text, including the provider flavour that sends the whole argument object as a dict
    (nothing incremental to report — such a call has no typewriter, only the final ``result``).
    """

    slot: int
    id: str | None = None
    name: str | None = None
    arguments: str | None = None


class StreamEvent(BaseModel):
    """One increment of a streaming completion.

    ``delta`` events carry a content token; ``reasoning`` events carry a chain-of-thought
    token (kept separate so the UI shows thinking without polluting the answer, ADR-0041);
    ``tool_call`` events carry a partial tool call as it streams (#654, ADR-0121); the final
    event carries the assembled ``result`` (full content, reasoning, and any tool calls
    accumulated from the stream).
    """

    delta: str | None = None
    reasoning: str | None = None
    tool_call: ToolCallFragment | None = None
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
