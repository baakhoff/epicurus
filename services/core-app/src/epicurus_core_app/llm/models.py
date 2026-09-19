"""Provider-agnostic types for the LLM gateway.

The chat shapes (``ChatMessage`` / ``ChatResult`` / ``Role``) are the shared chat
contract — re-exported here from ``epicurus_core`` (ADR-0021) so the gateway,
agent, and routes keep importing them from one place. The remaining types are
gateway-internal.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, NamedTuple

from pydantic import BaseModel

from epicurus_core import ChatMessage, ChatResult, Role
from epicurus_core_app.llm.errors import LocalRuntimeState

__all__ = [
    "ChatMessage",
    "ChatResult",
    "KeyState",
    "LocalRuntimeStatus",
    "ModelDetails",
    "ModelInfo",
    "ModelRole",
    "ModelWarmth",
    "PowerState",
    "ProviderInfo",
    "Role",
    "StreamEvent",
    "ToolCallFragment",
    "UsageEvent",
]


class LocalRuntimeStatus(BaseModel):
    """Whether this deployment has a local LLM runtime, and whether it answers (#962).

    The body of ``GET /platform/v1/llm/local-runtime``. A **separate** endpoint rather than an
    envelope around ``GET /llm/models``, which stays a bare ``list[ModelInfo]``: five web
    consumers and seven internal callers read that list, and wrapping it would be a breaking
    change bought for nothing (ADR-0144).

    ``url_configured`` is the *why* behind the state: false means ``OLLAMA_URL`` is blank — a
    deliberate hosted-only deployment — and no surface should draw a local-runtime control at
    all. It is false exactly when ``state`` is ``absent``.
    """

    state: LocalRuntimeState
    url_configured: bool


class ModelWarmth(NamedTuple):
    """What :meth:`LlmGateway.model_readiness` answers (ADR-0027, extended by #962).

    ``warm`` is ``None`` whenever local warm-up is not a question that applies — a hosted
    model, or a deployment with no local runtime at all — and ``runtime`` says which of the
    two, so the readiness probe reports ``n/a`` for the second instead of "warming" forever.
    """

    model: str
    warm: bool | None
    runtime: Literal["local", "hosted", "absent"] = "local"


ModelRole = Literal["chat", "embedding", "unknown"]
"""What a model is *for*, as the gateway resolved it (#944, ADR-0140).

``unknown`` is a first-class answer and the safe one: an id the catalogue has never heard of
is refused nothing, because a wrong "this is an embedding model" would lock the operator out
of a model that works. Only a *known* mismatch — a model the catalogue (or the operator) says
is an embedding model, asked to answer a chat turn — is refused.
"""


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
    Any field is ``None``/empty when the runtime did not report it (or the model isn't local).

    ``role``, ``supports_tools`` and ``in_catalogue`` are the **resolved** capability answers
    (ADR-0140) — the operator's override, what the gateway learned from the provider, and the
    catalogue, already merged. They exist because ``capabilities`` cannot express "unknown":
    an empty list has to mean both "this model can do nothing we badge" and "we have no idea",
    and a shell that guesses between them shows the wrong hint. ``supports_tools is None`` is
    the honest "we do not know"; ``in_catalogue`` is ``False`` for a hosted id LiteLLM's map has
    never heard of (``None`` for a local model, which has no such map) — the fact
    ``_note_unmapped`` has always logged and never surfaced (#879)."""

    quantization: str | None = None
    parameter_size: str | None = None
    context_length: int | None = None
    family: str | None = None
    capabilities: list[str] = []
    role: ModelRole = "unknown"
    supports_tools: bool | None = None
    in_catalogue: bool | None = None


KeyState = Literal["not_required", "present", "missing", "unavailable"]
"""What the secret store said about a provider's key.

Three real answers, not two: ``missing`` means OpenBao replied and has nothing at that path;
``unavailable`` means OpenBao could not be asked at all. Collapsing them is #728's
misdiagnosis — an expired app token made every hosted provider read as *unconfigured*, which
sends an operator hunting for a key they already set instead of at the token. ``not_required``
is the local runtime, which has no key to hold.
"""


class ProviderInfo(BaseModel):
    """A configured LLM provider and what the secret store knows about its key."""

    alias: str
    local: bool
    # True for `not_required` and `present`. Kept as-is — it is what the picker and the
    # Models page already read — with `key_state` carrying the distinction it cannot make.
    configured: bool
    # The "custom" (any-OpenAI-compatible) provider also needs an endpoint URL.
    needs_base_url: bool = False
    key_state: KeyState = "not_required"
    # Why the store could not answer, when `key_state` is `unavailable`. The message names
    # the path and the HTTP status, and a 403 already names token expiry as the likely cause
    # (#728). Never a credential — the store raises about access, not contents.
    key_error: str | None = None


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
