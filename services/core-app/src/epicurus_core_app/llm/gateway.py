"""The LLM gateway — the core's single entry point to language models (ADR-0010).

Targets the local Ollama runtime plus hosted providers (Claude, ChatGPT, Grok,
DeepSeek, Gemini, OpenRouter, and a generic OpenAI-compatible escape hatch) through the
LiteLLM SDK. Provider keys are fetched from OpenBao at call time (tenant-scoped) and never
logged. Both **chat and embeddings** route this way (#865) — an embedding model is
classified by the same provider registry, so a hosted embedding model is reachable
wherever a hosted chat model is.

Routing (ADR-0010): a request tries the chosen model, then the configured fallback
chain on failure. While the runtime is **paused** (ADR-0005), local models are
skipped — running one would wake the GPU — but hosted providers stay available, so a
hosted fallback still serves. Each call emits a usage event on NATS (no prompt
content, no keys). Retries on 429/5xx use LiteLLM's exponential backoff.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import litellm

from epicurus_core import EventBus, SecretError, SecretNotFoundError, SecretStore, get_logger
from epicurus_core_app.llm import providers as registry
from epicurus_core_app.llm.compaction import (
    compact_messages,
    estimate_tools_tokens,
    reply_reserve,
)
from epicurus_core_app.llm.errors import ModelCapabilityError
from epicurus_core_app.llm.model_settings import ModelSettings, ModelSettingsStore
from epicurus_core_app.llm.models import (
    ChatMessage,
    ChatResult,
    KeyState,
    ModelDetails,
    ModelInfo,
    ModelRole,
    ProviderInfo,
    StreamEvent,
    ToolCallFragment,
    UsageEvent,
)
from epicurus_core_app.llm.power import GatewayPausedError, PowerController
from epicurus_core_app.llm.prefs import LlmPrefsStore
from epicurus_core_app.llm.reasoning import ThinkSplitter, split_reasoning
from epicurus_core_app.llm.saved_models import SavedHostedModelStore, SavedModelOverride

litellm.telemetry = False
litellm.drop_params = True

log = get_logger("epicurus_core_app.llm")

USAGE_SUBJECT = "llm.usage"

# Inserted in place of dropped history when a turn is trimmed to fit the context window, so the
# model knows earlier messages were cut rather than never said.
_TRIM_NOTE = "(Earlier messages in this conversation were trimmed to fit the context window.)"

# Connect timeout (seconds) for every LLM call — bounds only the TCP/TLS handshake, so a down
# runtime or unreachable hosted endpoint fails fast regardless of how generous the read timeout is.
# (For a local ``ollama_chat`` call LiteLLM collapses our httpx.Timeout to its read component, so
# this connect only actually governs hosted providers; a down localhost Ollama refuses instantly.)
_CONNECT_TIMEOUT_S = 30.0

# Effective "no inter-chunk limit" — a very large but finite read timeout used when the operator
# sets LLM_TIMEOUT=0. It must be finite, not None: ``ollama_chat`` is not in LiteLLM's
# ``supports_httpx_timeout()`` allowlist, so ``CompletionTimeout.resolve()``
# (litellm_core_utils/completion_timeout.py) collapses our httpx.Timeout to its ``.read``
# component and substitutes its own ``COMPLETION_HTTP_FALLBACK_SECONDS`` (600s) whenever
# that component is ``None`` — verified by calling ``resolve()`` directly against the pinned
# litellm 1.89.3 (#453, #466). So "disable the bound" is expressed as a read that never
# realistically elapses rather than as no timeout at all.
_UNBOUNDED_READ_S = 365 * 24 * 60 * 60.0  # 1 year

# LiteLLM's ``mode`` values that mean "this model answers chat turns" (#944). Its map also
# carries ``embedding``, ``rerank``, ``image_generation``, ``audio_transcription`` and more;
# anything outside these two sets resolves to ``unknown`` and is refused nothing.
_CHAT_MODES = frozenset({"chat", "completion", "responses"})

# How a provider says "I will not take a tool list" (#947). Lower-cased substrings, matched
# against a 400's text only — a deliberately small, readable list rather than a per-provider
# table, because *the deployment* decides: the same model id behind a vLLM server started with
# ``--enable-auto-tool-choice`` is fully tool-capable. Nothing here is provider-specific beyond
# the phrasing; the rest of the rule (learn it, retry without tools) is generic.
_TOOL_REJECTION_MARKERS = (
    "tool choice requires",
    "tool_choice",
    "tools is not supported",
    "tools are not supported",
    "does not support tools",
    "tool use is not supported",
    "function calling is not supported",
    "does not support function calling",
)

# The upstream provider an aggregator names in its error body (OpenRouter's ``provider_name``,
# e.g. "NextBit"). A readable name, never an id: the character class is narrow on purpose so a
# token or account identifier in a malformed payload cannot ride into a log line. Used for the
# WARNING only — never rendered to a user.
_UPSTREAM_PROVIDER = re.compile(r'"provider_name"\s*:\s*"([A-Za-z0-9 ._-]{1,40})"')

# Appended to the system prefix of a turn that is offered no tools (#947). Without it the base
# prompt's "act through the tools you are given" is an invitation to narrate tool use that never
# happened. Lives here, beside the capability resolution that decides a turn is tool-less, so
# both the streamed path and the automation loop reach it through one call.
NO_TOOLS_SYSTEM_NOTE = (
    "This conversation has no tools available: the model answering it cannot call them. "
    "Answer from what you already know, say plainly when something would need a tool you do "
    "not have, and never claim to have read, created, changed or scheduled anything."
)


def with_no_tools_note(messages: list[ChatMessage]) -> list[ChatMessage]:
    """``messages`` plus :data:`NO_TOOLS_SYSTEM_NOTE`, inside the leading system prefix (#947).

    Inserted after the existing system messages rather than appended at the end, because that
    prefix is the block :func:`compaction.compact_messages` keeps whole — a note at the tail
    would be the first thing a long conversation drops, which is precisely the turn where a
    model is most likely to start inventing tool calls.
    """
    prefix = 0
    while prefix < len(messages) and messages[prefix].role == "system":
        prefix += 1
    note = ChatMessage(role="system", content=NO_TOOLS_SYSTEM_NOTE)
    return [*messages[:prefix], note, *messages[prefix:]]


def _role_from_mode(mode: object) -> ModelRole:
    """Map LiteLLM's catalogue ``mode`` to the gateway's :data:`ModelRole` (#944)."""
    if mode == "embedding":
        return "embedding"
    if isinstance(mode, str) and mode in _CHAT_MODES:
        return "chat"
    return "unknown"


def _is_bad_request(exc: Exception) -> bool:
    """Whether ``exc`` is a provider **400** — a deterministic refusal, not a transient fault.

    Read off ``status_code`` rather than by class: ``litellm.BadRequestError`` carries one (it
    subclasses openai's ``APIStatusError``), and so does anything else that reaches here from a
    provider SDK, so the duck-typed check covers the class without pinning this module to
    LiteLLM's export surface.
    """
    return getattr(exc, "status_code", None) == 400


def _tool_rejection_phrase(exc: Exception) -> str | None:
    """The :data:`_TOOL_REJECTION_MARKERS` phrase ``exc`` matched, or ``None`` (#947).

    Restricted to a 400 on purpose: a 500 mentioning tools is the provider falling over, not the
    model declaring a limitation, and learning "no tool support" from an outage would disable
    every module until the operator noticed. A non-tool 400 — #944's "is an embedding model and
    cannot be used with the chat/completions endpoint" — matches nothing here and is re-raised.
    """
    if not _is_bad_request(exc):
        return None
    text = str(exc).lower()
    return next((marker for marker in _TOOL_REJECTION_MARKERS if marker in text), None)


def _upstream_provider_name(text: str) -> str | None:
    """The upstream provider an aggregator named in its error body, if it named one (#947)."""
    match = _UPSTREAM_PROVIDER.search(text)
    return match.group(1) if match else None


def _build_stream_timeout(read_timeout_s: float) -> httpx.Timeout:
    """The httpx timeout for LLM calls: a generous read, a short connect (#453).

    ``read_timeout_s`` is the inter-chunk read deadline (``LLM_TIMEOUT``); ``0`` (or negative)
    means "no inter-chunk bound" and maps to :data:`_UNBOUNDED_READ_S`. LiteLLM threads the read
    component down to aiohttp's ``sock_read`` (which fires on the gap *between* stream chunks),
    so this is what keeps a legitimate cold-model-load / prompt-eval stall from aborting the turn.
    """
    read = read_timeout_s if read_timeout_s and read_timeout_s > 0 else _UNBOUNDED_READ_S
    return httpx.Timeout(read, connect=_CONNECT_TIMEOUT_S)


def _normalize_arguments(raw: Any) -> str:
    """Coerce a tool call's ``arguments`` to exactly one valid JSON string.

    A provider replay (LiteLLM → Ollama) runs ``json.loads`` over every stored tool call
    when it builds the next request, so the value must be a single loadable JSON document.
    Two things break that: Ollama streams arguments as a dict, and a local model that emits
    the same call twice yields two concatenated objects (``{…}{…}``) — both surface on the
    *next* turn as ``JSONDecodeError: Extra data`` (an ``APIConnectionError`` that kills the
    turn). Repair to a canonical string here: a dict is dumped, a leading JSON value is
    salvaged from any trailing junk, and anything unparseable degrades to ``{}`` rather than
    poisoning the conversation. A value that is already one valid JSON string is returned
    verbatim (no re-encoding).
    """
    if isinstance(raw, dict):
        return json.dumps(raw)
    if not isinstance(raw, str) or not raw.strip():
        return "{}"
    try:
        json.loads(raw)
    except json.JSONDecodeError:
        try:  # salvage the first JSON value, dropping any trailing junk after it
            value, _ = json.JSONDecoder().raw_decode(raw.lstrip())
        except json.JSONDecodeError:
            return "{}"
        return json.dumps(value)
    return raw


def _normalize_tool_calls(
    tool_calls: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """Return ``tool_calls`` with every ``function.arguments`` a single valid JSON string.

    Guards the conversation against a malformed stream poisoning a later turn's replay
    (see :func:`_normalize_arguments`). A no-op for already-clean calls; copies rather than
    mutating the inputs.
    """
    if not tool_calls:
        return tool_calls
    normalized: list[dict[str, Any]] = []
    for call in tool_calls:
        function = {**(call.get("function") or {})}
        function["arguments"] = _normalize_arguments(function.get("arguments"))
        normalized.append({**call, "function": function})
    return normalized


class UnknownProviderError(LookupError):
    """Raised when a provider alias does not exist or cannot hold a key."""


class LlmGateway:
    """Unified, provider-agnostic access to language models."""

    def __init__(
        self,
        *,
        ollama_url: str,
        default_model: str,
        default_embed_model: str = "nomic-embed-text",
        keep_alive: str,
        power: PowerController,
        secrets: SecretStore,
        default_tenant: str,
        bus: EventBus,
        fallbacks: list[str],
        num_retries: int = 2,
        timeout: float = 600.0,
        temperature: float | None = None,
        top_p: float | None = None,
        num_ctx: int | None = None,
        prefs: LlmPrefsStore | None = None,
        model_settings: ModelSettingsStore | None = None,
        saved_models: SavedHostedModelStore | None = None,
    ) -> None:
        self._ollama_url = ollama_url.rstrip("/")
        self._default_model = default_model
        self._default_embed_model = default_embed_model
        self._keep_alive = keep_alive
        self._power = power
        self._secrets = secrets
        self._default_tenant = default_tenant
        self._bus = bus
        self._fallbacks = list(fallbacks)
        self._num_retries = num_retries
        # Inter-chunk read timeout for every LLM call, sized for local inference (#453). Built once
        # here (constant per gateway) and passed to each ``litellm.acompletion`` so a legitimate
        # cold-load / prompt-eval stall does not abort the stream at aiohttp's ``sock_read``.
        self._timeout = _build_stream_timeout(timeout)
        self._temperature = temperature
        self._top_p = top_p
        self._num_ctx = num_ctx
        self._prefs = prefs
        self._model_settings = model_settings
        self._saved_models = saved_models
        # Model ids already reported as absent from LiteLLM's static cost map (#711). An
        # operator-saved alias outside that map is *expected*, so the miss is worth exactly one
        # warning per id per process — enough to explain a model with no badges, not enough to
        # be noise. Capped because ``model`` reaches here from a route query param.
        self._unmapped_models: set[str] = set()
        # Whether the override store has already failed once this process (#711). Unlike a
        # cost-map miss this is *not* expected, and it disables every override at once, so it
        # warns the first time rather than only at debug.
        self._override_store_failed = False
        # Local models whose role (chat vs embedding) the runtime has already answered (#944).
        # The role gate runs on *every* call, including each embed batch during a bulk re-index,
        # and for a local model the answer costs an ``/api/show`` round trip — so a definite
        # answer is remembered. A model's role is a property of its weights, and the two ways it
        # could change under us (a re-pull, a delete) both clear this. An ``unknown`` is never
        # cached: that is usually an unreachable runtime, and pinning it would keep the gate
        # blind for the rest of the process. Bounded, like ``_unmapped_models``.
        self._local_roles: dict[str, ModelRole] = {}

    async def effective_default(self, tenant_id: str | None = None) -> str:
        """The active default model: the stored pref if set, else the env default."""
        if self._prefs is not None:
            stored = await self._prefs.get_default(tenant_id or self._default_tenant)
            if stored:
                return stored
        return self._default_model

    async def effective_embed_default(self, tenant_id: str | None = None) -> str:
        """The active embedding model: the stored embed pref if set, else the env default.

        Symmetric with :meth:`effective_default` for chat. Callers that don't pass an
        explicit ``model`` to :meth:`embed` (e.g. core memory recall) resolve through here,
        so the operator's UI **Embedding model** choice actually drives embedding instead
        of a hard-coded setting.
        """
        if self._prefs is not None:
            stored = await self._prefs.get_embed_default(tenant_id or self._default_tenant)
            if stored:
                return stored
        return self._default_embed_model

    async def effective_context_window(self, tenant_id: str | None = None) -> int | None:
        """The active Ollama context window (num_ctx): the stored pref if set, else the env default.

        Symmetric with :meth:`effective_default` for chat. Resolved per turn so the operator's
        UI **Context window** choice drives ``num_ctx`` (the fix for the 4096-default context
        filling with the prompt and leaving no room to generate). ``None`` falls through to the
        runtime's own default — local models only; ignored by hosted providers.
        """
        if self._prefs is not None:
            stored = await self._prefs.get_context_window(tenant_id or self._default_tenant)
            if stored is not None:
                return stored
        return self._num_ctx

    async def effective_kv_cache_type(self, tenant_id: str | None = None) -> str | None:
        """The operator's Ollama KV-cache type (``f16``/``q8_0``/``q4_0``), or ``None``.

        Server-wide and applied via the Ollama container's ``OLLAMA_KV_CACHE_TYPE`` env (#310),
        not a per-call option — but the context-window suggestion reads it here so a quantized
        cache (which stores fewer bytes per token) is reflected as more usable context.
        """
        if self._prefs is None:
            return None
        return await self._prefs.get_kv_cache_type(tenant_id or self._default_tenant)

    async def _settings_for(self, model: str, tenant_id: str | None) -> ModelSettings:
        """The operator's per-model settings for ``model`` (empty when none apply).

        The store is keyed by the name the runtime reports (e.g. ``llama3.2:latest``), but a
        request may name the model bare (``llama3.2``) or vice-versa. Match loosely: exact
        name, then bare name, then the family (everything before the ``:tag``) — so a single
        sheet edit reliably reaches the model however it's addressed. Hosted ids carry a
        ``provider/`` prefix which we strip before matching (these settings are local-only).
        """
        if self._model_settings is None:
            return ModelSettings()
        stored = await self._model_settings.list(tenant_id or self._default_tenant)
        if not stored:
            return ModelSettings()
        bare = model.split("/", 1)[-1]
        if model in stored:
            return stored[model]
        if bare in stored:
            return stored[bare]
        family = bare.split(":", 1)[0]
        for key, settings in stored.items():
            if key.split(":", 1)[0] == family:
                return settings
        return ModelSettings()

    async def model_readiness(
        self, model: str | None = None, *, tenant_id: str | None = None
    ) -> tuple[str, bool | None]:
        """Report whether a model is ready to answer *now* (ADR-0027).

        Returns ``(resolved_model, warm)``. ``warm`` is ``None`` for hosted providers — they
        need no local warm-up, so they are always ready; for the local runtime it is ``True``
        only when the model is already loaded in memory (``False`` while paused, or cold).
        Best-effort: a runtime probe failure reports the model as cold rather than raising.
        """
        resolved = model or await self.effective_default(tenant_id)
        _, provider = registry.resolve(resolved)
        if not provider.is_local:
            return resolved, None
        if self._power.paused:
            return resolved, False
        target = resolved.split("/", 1)[-1]  # a bare local name has no prefix; this is a no-op
        try:
            loaded = {info.name for info in await self.models(tenant_id) if info.loaded}
        except Exception:  # runtime unreachable — treat as cold, never raise into readiness
            log.warning("model readiness probe failed; reporting cold", model=resolved)
            return resolved, False
        # The runtime tags loaded models (e.g. "llama3.2:latest"); match the bare name too.
        warm = target in loaded or any(name.split(":", 1)[0] == target for name in loaded)
        return resolved, warm

    def _candidates(self, model: str) -> list[str]:
        """The chosen model followed by the configured fallback chain (deduped)."""
        ordered = [model]
        for fallback in self._fallbacks:
            if fallback not in ordered:
                ordered.append(fallback)
        return ordered

    def _is_available(self, model: str) -> bool:
        """Unavailable only if local while paused — running it would wake the GPU.

        Hosted providers stay available when paused (they use no local GPU).
        """
        _, provider = registry.resolve(model)
        return not (self._power.paused and provider.is_local)

    async def _ensure_can_serve(
        self,
        model: str,
        *,
        want: ModelRole,
        tenant_id: str | None,
        check_pause: bool = True,
    ) -> None:
        """The one gate every inference entry point passes through (ADR-0140).

        Two rules today, and the place the third goes — the system-level Local AI / Hosted AI
        switches (#945) add a clause *here*, not a fourth copy scattered across the call sites:

        * **Paused** (ADR-0005): a local model cannot run while the runtime is paused.
        * **Role** (#944): a model whose role is *known* and is not ``want`` is refused before
          any provider call, with a :class:`ModelCapabilityError` naming the one action that
          fixes it. ``unknown`` is refused nothing — a catalogue miss must never lock the
          operator out of a model that works.

        ``check_pause=False`` is what the chat paths pass, and it is not a weakening: they
        express the pause as a *fallback filter* (:meth:`_is_available` over
        :meth:`_candidates`), so a paused local default falls through to a hosted fallback
        instead of failing, and the candidate this is called about has already passed it.
        :meth:`embed` has no fallback chain, so it takes the whole gate — which is what retires
        the inline copy of the pause rule that used to live there.
        """
        if check_pause and not self._is_available(model):
            raise GatewayPausedError("LLM gateway is paused; resume to run inference")
        role = await self.model_role(model, tenant_id)
        if role == "unknown" or role == want:
            return
        if want == "chat":
            raise ModelCapabilityError(
                model=model,
                capability="chat",
                message=f"{model} is an embedding model, so it can't answer a chat turn.",
                hint="Pick a chat model on the Models page and star it as the default.",
            )
        raise ModelCapabilityError(
            model=model,
            capability="embedding",
            message=f"{model} is a chat model, so it can't produce embeddings.",
            hint="Pick an embedding model under Models → Embedding model.",
        )

    async def model_role(self, model: str | None = None, tenant_id: str | None = None) -> ModelRole:
        """What ``model`` is *for* — ``chat``, ``embedding``, or ``unknown`` (#944).

        Resolved by :meth:`show` (operator override → catalogue), so there is one place that
        answers a capability question. A **local** answer is memoised per process: this runs on
        every call, and for a local model the catalogue is the runtime — one ``/api/show`` per
        embed batch during a bulk re-index is a cost worth paying once. Hosted resolution is a
        static map lookup plus the tenant's stored override, so it is not cached: an override
        edited on the Models page takes effect on the next turn.
        """
        resolved = model or await self.effective_default(tenant_id)
        _, provider = registry.resolve(resolved)
        if not provider.is_local:
            return (await self.show(resolved, tenant_id)).role
        cached = self._local_roles.get(resolved)
        if cached is not None:
            return cached
        role = (await self.show(resolved, tenant_id)).role
        if role != "unknown":
            if len(self._local_roles) >= 512:  # bounded: ``model`` can arrive as a query param
                self._local_roles.clear()
            self._local_roles[resolved] = role
        return role

    async def _call_config(self, model: str, tenant_id: str | None) -> dict[str, Any]:
        """The LiteLLM call kwargs (model, endpoint, key, tuning) for ``model``.

        For hosted providers the API key is fetched from OpenBao at call time and is never
        logged. For local models the Ollama runtime options are resolved **per this model**:
        ``num_ctx`` from the operator's per-model setting, else the global context-window
        pref, else the env default; ``keep_alive`` from the per-model setting, else the env
        default. So a small model and a large one can carry different context windows and
        keep-alives. Sampling knobs (temperature/top_p) come from settings.
        """
        litellm_model, provider = registry.resolve(model)
        config: dict[str, Any] = {"model": litellm_model}
        if provider.is_local:
            config["api_base"] = self._ollama_url
            settings = await self._settings_for(model, tenant_id)
            num_ctx = await self._effective_num_ctx(model, tenant_id, settings=settings)
            # num_ctx is an Ollama runtime option — local models only.
            if num_ctx is not None:
                config["num_ctx"] = num_ctx
            config["keep_alive"] = settings.keep_alive or self._keep_alive
            # device → Ollama num_gpu (layers offloaded to the GPU): "cpu" = 0 (all CPU),
            # "gpu" = 999 (all layers; the runtime clamps to the model's count), "auto"/unset
            # = omit so the runtime decides. Lets the operator pin where a model runs (#293).
            if settings.device == "cpu":
                config["num_gpu"] = 0
            elif settings.device == "gpu":
                config["num_gpu"] = 999
        if provider.secret_path is not None:
            tenant = tenant_id or self._default_tenant
            secret = await self._secrets.get(provider.secret_path, tenant)
            config["api_key"] = secret["api_key"]
            if provider.needs_base_url:
                config["api_base"] = secret["api_base"]
        # Sampling knobs apply to every provider; LiteLLM (drop_params=True) drops
        # any that a given provider does not support.
        if self._temperature is not None:
            config["temperature"] = self._temperature
        if self._top_p is not None:
            config["top_p"] = self._top_p
        return config

    async def _effective_num_ctx(
        self, model: str, tenant_id: str | None, *, settings: ModelSettings | None = None
    ) -> int | None:
        """The Ollama context window for ``model``: per-model setting, else global pref, else env.

        One source of truth for both the runtime ``num_ctx`` option and the context-fit budget.
        ``None`` means no explicit window (the runtime's own default applies).
        """
        if settings is None:
            settings = await self._settings_for(model, tenant_id)
        if settings.context_window is not None:
            return settings.context_window
        return await self.effective_context_window(tenant_id)

    async def _hosted_budget(self, model: str, tenant_id: str | None) -> int | None:
        """The compaction budget for a hosted model: its per-model context window, else ``None``.

        A hosted provider fixes the real window and *rejects* an over-window request, so the
        operator's per-model setting is a **budget** — the size a long conversation is trimmed to
        (:meth:`_fit_to_context`), which both averts that rejection and caps per-turn input spend
        (#570). ``None`` means no budget: today's untouched behavior.

        Deliberately **not** :meth:`_effective_num_ctx`: that falls through to the global Ollama
        ``num_ctx`` pref, a *local* runtime allocation, and a stored 8k local value must never
        silently over-compact a 200k hosted window. The lookup is **exact** (the full hosted id),
        not the loose family match of :meth:`_settings_for`, so a local model's settings can't
        bleed in either (a hosted ``custom/llama3.2`` must not inherit a local ``llama3.2:latest``
        window).
        """
        if self._model_settings is None:
            return None
        settings = await self._model_settings.get(tenant_id or self._default_tenant, model)
        return settings.context_window

    async def _fit_to_context(
        self,
        model: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None,
        tenant_id: str | None,
    ) -> list[ChatMessage]:
        """Trim ``messages`` to fit ``model``'s context window before the call.

        The window is a **runtime allocation** for local models (``num_ctx`` → KV-cache memory;
        the runtime silently drops tokens past it, evicting the oldest — the system prompt +
        recalled context — so we pre-trim) and a **budget** for hosted ones (the provider fixes
        the real window and rejects an over-window request, so the per-model setting both prevents
        that overflow and caps per-turn input spend, #570). Either way, when a window applies we
        keep the system prefix and the most-recent turns within it minus a reply reserve and the
        tool schemas' footprint (see :mod:`compaction`); with no window set, messages are untouched.

        The two classes resolve the window differently: local is per-model → global pref → env
        (:meth:`_effective_num_ctx`); hosted is the per-model setting **only**
        (:meth:`_hosted_budget`) — never the global Ollama pref, which is a local-only knob.
        """
        _, provider = registry.resolve(model)
        if provider.is_local:
            window = await self._effective_num_ctx(model, tenant_id)
        else:
            window = await self._hosted_budget(model, tenant_id)
        if not window:
            return messages
        budget = window - reply_reserve(window) - estimate_tools_tokens(tools)
        return compact_messages(messages, budget=budget, note=_TRIM_NOTE)

    async def _acompletion(
        self,
        *,
        model: str,
        config: dict[str, Any],
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None,
        tenant_id: str | None,
        stream: bool = False,
    ) -> Any:
        """Call LiteLLM, and learn from a provider that refuses the tool list (#947).

        A tool list is a *request* to the model, and whether it can be honoured is a property of
        the model **as served** — the same id behind a vLLM server started without
        ``--enable-auto-tool-choice`` rejects every request that carries one, while the same id
        elsewhere calls tools happily. No static table can answer that, so the gateway learns it
        from the only authority there is: the refusal itself. On a 400 whose text matches
        :func:`_tool_rejection_phrase`, it records ``tools=off`` on the caller's saved row (for
        the **calling** tenant — a provider's deployment is a per-tenant fact, since the key is),
        says why at WARNING, and **retries the same call once with ``tools`` omitted**, so the
        turn answers instead of dying. Once only: the retry carries no tools, so a second
        rejection is a real failure and propagates.

        Anything else — including #944's "is an embedding model" 400 — is re-raised untouched.
        """
        kwargs: dict[str, Any] = {
            "messages": [m.provider_dump() for m in messages],
            "num_retries": self._num_retries,
            "timeout": self._timeout,
            **config,
        }
        if stream:
            kwargs["stream"] = True
        try:
            return await litellm.acompletion(tools=tools, **kwargs)
        except Exception as exc:
            phrase = _tool_rejection_phrase(exc) if tools else None
            if phrase is None:
                raise
            await self._learn_tools_unsupported(model, tenant_id, phrase=phrase, detail=str(exc))
            return await litellm.acompletion(tools=None, **kwargs)

    async def _learn_tools_unsupported(
        self, model: str, tenant_id: str | None, *, phrase: str, detail: str
    ) -> None:
        """Record (and explain) a provider's refusal of a tool list (#947).

        The WARNING names the model, the provider alias, the upstream the aggregator named, and
        the phrase that matched — enough to tell "this deployment has tool calling switched off"
        from a genuine outage without reading the raw body, and nothing that could be an account
        id or a key. Persisting is best-effort: the retry-without-tools is what rescues *this*
        turn, and a store hiccup must not turn a recoverable turn into a failed one. Nothing is
        recorded for a model the tenant has not saved (a local id, or one used but not persisted)
        — there is no row to carry the fact, and a local runtime answers for itself.
        """
        alias = model.partition("/")[0] if registry.is_hosted(model) else "local"
        log.warning(
            "model rejected the tool list; retrying without tools",
            model=model,
            provider=alias,
            upstream=_upstream_provider_name(detail),
            matched=phrase,
        )
        if self._saved_models is None:
            return
        try:
            await self._saved_models.learn_tools_unsupported(
                tenant_id or self._default_tenant, model
            )
        except Exception as exc:  # the turn is already rescued; remembering is a bonus
            log.warning("could not record the learned tool capability", model=model, error=str(exc))

    async def _complete(
        self,
        model: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None,
        tenant_id: str | None,
        automation_id: str | None = None,
    ) -> ChatResult:
        config = await self._call_config(model, tenant_id)
        messages = await self._fit_to_context(model, messages, tools, tenant_id)
        start = time.monotonic()
        response = await self._acompletion(
            model=model,
            config=config,
            messages=messages,
            tools=tools,
            tenant_id=tenant_id,
        )
        latency_ms = (time.monotonic() - start) * 1000
        self._power.mark_active()
        data: dict[str, Any] = response.model_dump()
        message = data["choices"][0]["message"]
        usage = data.get("usage") or {}
        # Reasoning is either a separate field (hosted reasoning models) or inlined in the
        # content as <think>…</think> (local models); take the native field if present, else
        # split it out so the answer stays clean (ADR-0041).
        answer, inline_thinking = split_reasoning(message.get("content") or "")
        reasoning = message.get("reasoning_content") or inline_thinking or None
        result = ChatResult(
            model=data.get("model") or config["model"],
            content=answer,
            tool_calls=_normalize_tool_calls(message.get("tool_calls")),
            reasoning=reasoning,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )
        await self._emit_usage(
            model=result.model,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            latency_ms=latency_ms,
            tenant_id=tenant_id,
            automation_id=automation_id,
        )
        return result

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        tenant_id: str | None = None,
        automation_id: str | None = None,
    ) -> ChatResult:
        """Return a completion, walking the fallback chain on failure.

        A model whose role is known *not* to be chat is refused here, before the chain is
        walked (#944): the fallback chain exists for a provider that is failing, not for a
        default that is wrong, and quietly answering from a fallback would hide exactly the
        misconfiguration the operator needs to see.
        """
        resolved = model or await self.effective_default(tenant_id)
        await self._ensure_can_serve(resolved, want="chat", tenant_id=tenant_id, check_pause=False)
        last_error: Exception | None = None
        for candidate in self._candidates(resolved):
            if not self._is_available(candidate):
                continue
            try:
                return await self._complete(candidate, messages, tools, tenant_id, automation_id)
            except Exception as exc:  # provider/call error -> try the next candidate
                last_error = exc
                log.warning("llm call failed; trying next", model=candidate, error=str(exc))
        if last_error is not None:
            raise last_error
        raise GatewayPausedError("LLM gateway is paused; no non-local model is available")

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        tenant_id: str | None = None,
    ) -> AsyncIterator[str]:
        """Yield content deltas from the first available candidate."""
        resolved = model or await self.effective_default(tenant_id)
        await self._ensure_can_serve(resolved, want="chat", tenant_id=tenant_id, check_pause=False)
        candidate = next((c for c in self._candidates(resolved) if self._is_available(c)), None)
        if candidate is None:
            raise GatewayPausedError("LLM gateway is paused; no non-local model is available")
        config = await self._call_config(candidate, tenant_id)
        messages = await self._fit_to_context(candidate, messages, None, tenant_id)
        start = time.monotonic()
        response = await self._acompletion(
            model=candidate,
            config=config,
            messages=messages,
            tools=None,
            tenant_id=tenant_id,
            stream=True,
        )
        self._power.mark_active()
        async for chunk in response:
            choices = chunk.choices
            if choices and (piece := choices[0].delta.content):
                yield piece
        await self._emit_usage(
            model=config["model"],
            prompt_tokens=None,
            completion_tokens=None,
            latency_ms=(time.monotonic() - start) * 1000,
            tenant_id=tenant_id,
        )

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        tenant_id: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a completion: ``delta`` events per token, then one ``result`` event.

        Tool-call fragments are assembled across chunks, so the final event's
        ``result.tool_calls`` is complete — the agent loop streams every round. Each fragment
        is *also* surfaced as it arrives, as a ``tool_call`` event (#654, ADR-0121), for a
        consumer that wants to watch a call being written; the assembly and the final
        ``result`` are untouched by that, so ignoring those events is the old behaviour
        exactly. Uses the first available candidate (no mid-stream fallback).
        """
        resolved = model or await self.effective_default(tenant_id)
        await self._ensure_can_serve(resolved, want="chat", tenant_id=tenant_id, check_pause=False)
        candidate = next((c for c in self._candidates(resolved) if self._is_available(c)), None)
        if candidate is None:
            raise GatewayPausedError("LLM gateway is paused; no non-local model is available")
        config = await self._call_config(candidate, tenant_id)
        messages = await self._fit_to_context(candidate, messages, tools, tenant_id)
        start = time.monotonic()
        response = await self._acompletion(
            model=candidate,
            config=config,
            messages=messages,
            tools=tools,
            tenant_id=tenant_id,
            stream=True,
        )
        self._power.mark_active()
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        splitter = ThinkSplitter()
        calls: dict[int, dict[str, Any]] = {}
        slot = 0  # active accumulator slot for providers that stream no fragment index
        async for chunk in response:
            choices = chunk.choices
            if not choices:
                continue
            delta = choices[0].delta
            # Hosted reasoning models stream a separate reasoning_content field.
            native_reasoning = getattr(delta, "reasoning_content", None)
            if native_reasoning:
                reasoning_parts.append(native_reasoning)
                yield StreamEvent(reasoning=native_reasoning)
            if delta.content:
                # Local models inline thinking as <think>…</think>; split it from the answer.
                answer_delta, think_delta = splitter.feed(delta.content)
                if think_delta:
                    reasoning_parts.append(think_delta)
                    yield StreamEvent(reasoning=think_delta)
                if answer_delta:
                    content_parts.append(answer_delta)
                    yield StreamEvent(delta=answer_delta)
            for fragment in delta.tool_calls or []:
                function = getattr(fragment, "function", None)
                name = getattr(function, "name", None) if function is not None else None
                # Choose the slot this fragment accumulates into. OpenAI streams one call as
                # partial fragments that share an `index` (continuations carry no name), so
                # those must coalesce. Ollama streams each *complete* call with a name but no
                # index (LiteLLM leaves it unset) — honoring `index or 0` collapsed them all
                # into slot 0 and concatenated their argument strings into invalid JSON, which
                # then crashed the next turn on replay ("Extra data"). So an un-indexed
                # fragment that names a tool starts a fresh slot instead of overwriting.
                if fragment.index is not None:
                    slot = fragment.index
                elif name and calls:
                    slot = max(calls) + 1
                entry = calls.setdefault(
                    slot, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                )
                if fragment.id:
                    entry["id"] = fragment.id
                argument_delta: str | None = None
                if function is not None:
                    if name:
                        entry["function"]["name"] = name
                    arguments = function.arguments
                    if isinstance(arguments, str):
                        entry["function"]["arguments"] += arguments
                        argument_delta = arguments or None
                    elif arguments is not None:  # some providers send whole args as a dict
                        # Replaced, not appended — so there is no *delta* to report; such a call
                        # only ever surfaces whole, on the final result.
                        entry["function"]["arguments"] = arguments
                # Surface the fragment as it arrives (#654), reusing the slot the accumulator
                # just chose rather than re-deriving it — the index discipline above is the
                # single source of truth for "which call is this". A fragment that carried
                # nothing new (no id, no name, no argument text) is not an event.
                if fragment.id or name or argument_delta:
                    yield StreamEvent(
                        tool_call=ToolCallFragment(
                            slot=slot,
                            id=entry["id"] or None,
                            name=entry["function"]["name"] or None,
                            arguments=argument_delta,
                        )
                    )
        # Release any tail the splitter was holding back in case it began a <think> tag.
        answer_tail, think_tail = splitter.flush()
        if think_tail:
            reasoning_parts.append(think_tail)
            yield StreamEvent(reasoning=think_tail)
        if answer_tail:
            content_parts.append(answer_tail)
            yield StreamEvent(delta=answer_tail)
        result = ChatResult(
            model=config["model"],
            content="".join(content_parts),
            tool_calls=_normalize_tool_calls([calls[i] for i in sorted(calls)]) or None,
            reasoning="".join(reasoning_parts) or None,
        )
        yield StreamEvent(result=result)
        await self._emit_usage(
            model=config["model"],
            prompt_tokens=None,
            completion_tokens=None,
            latency_ms=(time.monotonic() - start) * 1000,
            tenant_id=tenant_id,
        )

    async def _emit_usage(
        self,
        *,
        model: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        latency_ms: float,
        tenant_id: str | None,
        automation_id: str | None = None,
    ) -> None:
        """Publish a usage event on NATS. Best-effort — never breaks inference.

        ``automation_id`` carries the **second** attribution (ADR-0105): a run's inference
        is billed to the tenant *and* to the automation that caused it. Constraint #1 asks
        that every metering path name its tenant; an automation adds the only other thing
        an operator (or the SaaS overlay) needs to answer "what is this costing me, and
        which of my automations is doing it?". ``None`` on every ordinary turn.
        """
        tenant = tenant_id or self._default_tenant
        event = UsageEvent(
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=round(latency_ms),
            tenant=tenant,
            automation_id=automation_id,
        )
        try:
            await self._bus.publish(USAGE_SUBJECT, event.model_dump(), tenant_id=tenant)
        except Exception:  # usage accounting must never break inference
            log.warning("usage event publish failed", exc_info=True)

    async def _embed_config(self, model: str, tenant_id: str | None) -> dict[str, Any]:
        """The LiteLLM ``aembedding`` kwargs for ``model`` — local runtime or hosted provider.

        Classified through the same registry as chat (:func:`providers.resolve`), so exactly
        one rule decides what "hosted" means across the gateway.

        **Local.** The model goes to LiteLLM's ``ollama`` embeddings route — *not* the
        ``ollama_chat`` route ``resolve`` picks for completions, which has no embeddings
        dispatch — so the resolved prefix is swapped for ``ollama/``. That also normalises an
        explicit ``local/nomic-embed-text`` id, which used to be passed through verbatim and
        reach the runtime as the nonexistent model ``local/nomic-embed-text``. The operator's
        per-model settings sheet then supplies the Ollama runtime options (context window,
        keep-alive, device); with nothing set, the call is unchanged — embeddings stay opt-in,
        never silently retuned.

        **Hosted.** The tenant-scoped API key is fetched from OpenBao at call time and never
        logged, exactly as :meth:`_call_config` does for chat, and the generic
        OpenAI-compatible provider also reads its ``api_base`` from the secret. No Ollama
        runtime option is sent: ``num_ctx`` / ``keep_alive`` / ``num_gpu`` describe a local
        allocation and mean nothing to a provider. A missing key raises the same
        ``SecretNotFoundError`` the chat path raises, so both surfaces report an unconfigured
        provider identically.
        """
        litellm_model, provider = registry.resolve(model)
        if provider.is_local:
            bare = litellm_model.partition("/")[2]
            config: dict[str, Any] = {"model": f"ollama/{bare}", "api_base": self._ollama_url}
            settings = await self._settings_for(model, tenant_id)
            if settings.context_window is not None:
                config["num_ctx"] = settings.context_window
            if settings.keep_alive:
                config["keep_alive"] = settings.keep_alive
            if settings.device == "cpu":
                config["num_gpu"] = 0
            elif settings.device == "gpu":
                config["num_gpu"] = 999
            return config
        config = {"model": litellm_model}
        if provider.secret_path is not None:  # always true for a hosted provider
            secret = await self._secrets.get(
                provider.secret_path, tenant_id or self._default_tenant
            )
            config["api_key"] = secret["api_key"]
            if provider.needs_base_url:
                config["api_base"] = secret["api_base"]
        return config

    async def embed(
        self, texts: list[str], *, model: str | None = None, tenant_id: str | None = None
    ) -> list[list[float]]:
        """Embed ``texts`` with the resolved embedding model — local or hosted (#865).

        The model id is classified by the provider registry, exactly as a chat model is: a
        bare name or ``local/…`` runs on the local Ollama runtime; a known hosted alias
        (``gpt/text-embedding-3-small``, ``openrouter/openai/text-embedding-3-small``, …) goes
        to that provider through LiteLLM with the tenant's key from OpenBao. See
        :meth:`_embed_config` for what each class sends.

        Both the pause rule and the role rule are asked of :meth:`_ensure_can_serve`, the one
        gate every entry point shares (ADR-0140) — this path used to carry its own copy of the
        pause check, which is how the two could drift. **Pause (ADR-0005) applies to local
        models only**: a paused runtime refuses a local embed (running one would wake the GPU)
        while a hosted embed still serves, so memory recall and module indexing keep working on
        a paused box. ``mark_active`` is called either way — as the chat path does for a hosted
        completion — and is a no-op while paused, so it can never wake anything. A model known
        to be a *chat* model is refused outright (#944), the mirror of the chat path's refusal
        of an embedding model.

        Either way the call emits the same tenant-scoped usage event (constraint #1), naming
        the model actually called.

        Time-boxed to the same ``LLM_TIMEOUT``-derived bound the chat/stream call sites carry
        (#453) — but enforced with :func:`asyncio.wait_for` rather than litellm's own
        ``timeout=`` kwarg. LiteLLM 1.89.3's ``ollama`` embeddings dispatch
        (``llms/ollama/completion/handler.py``'s ``ollama_aembeddings``) never threads
        ``timeout`` through to its HTTP call — unlike the chat path, where it reaches
        aiohttp's ``sock_read`` — so passing it as a kwarg here would be silently inert. The
        one bound covers hosted calls too rather than splitting the rule per provider.
        Cross-chat recall wraps its own, much shorter, gracefully-degrading budget on top of
        this (``agent._recall_within_budget``); this guard covers the direct/module paths
        that had no bound at all (#466).
        """
        resolved = model or await self.effective_embed_default(tenant_id)
        await self._ensure_can_serve(resolved, want="embedding", tenant_id=tenant_id)
        config = await self._embed_config(resolved, tenant_id)
        start = time.monotonic()
        response = await asyncio.wait_for(
            litellm.aembedding(input=texts, **config),
            timeout=self._timeout.read,
        )
        self._power.mark_active()
        await self._emit_usage(
            model=str(config["model"]),
            prompt_tokens=None,
            completion_tokens=None,
            latency_ms=(time.monotonic() - start) * 1000,
            tenant_id=tenant_id,
        )
        data: dict[str, Any] = response.model_dump()
        return [item["embedding"] for item in data["data"]]

    async def set_provider_key(
        self,
        alias: str,
        *,
        api_key: str,
        api_base: str | None = None,
        tenant_id: str | None = None,
    ) -> None:
        """Store a hosted provider's API key in OpenBao (tenant-scoped).

        The key is held only by the secret store — never logged, never returned.
        """
        provider = registry.PROVIDERS.get(alias)
        if provider is None or provider.secret_path is None:
            raise UnknownProviderError(f"no hosted provider named {alias!r}")
        if provider.needs_base_url and not api_base:
            raise ValueError(f"provider {alias!r} needs an api_base (OpenAI-compatible endpoint)")
        data: dict[str, Any] = {"api_key": api_key}
        if api_base:
            data["api_base"] = api_base
        await self._secrets.set(provider.secret_path, data, tenant_id or self._default_tenant)

    async def clear_provider_key(self, alias: str, *, tenant_id: str | None = None) -> None:
        """Remove a hosted provider's stored API key."""
        provider = registry.PROVIDERS.get(alias)
        if provider is None or provider.secret_path is None:
            raise UnknownProviderError(f"no hosted provider named {alias!r}")
        await self._secrets.delete(provider.secret_path, tenant_id or self._default_tenant)

    async def providers(self, tenant_id: str | None = None) -> list[ProviderInfo]:
        """List the providers and what the secret store knows about each one's key."""
        tenant = tenant_id or self._default_tenant
        infos: list[ProviderInfo] = []
        for alias, provider in registry.PROVIDERS.items():
            state, detail = await self._key_state(provider.secret_path, tenant)
            infos.append(
                ProviderInfo(
                    alias=alias,
                    local=provider.is_local,
                    configured=state in ("not_required", "present"),
                    needs_base_url=provider.needs_base_url,
                    key_state=state,
                    key_error=detail,
                )
            )
        return infos

    async def _key_state(self, secret_path: str | None, tenant: str) -> tuple[KeyState, str | None]:
        """Whether the provider's key is there — or whether we could even ask (#728).

        The two failures are not the same fact and must not report the same one. OpenBao
        answering "nothing at that path" means the operator has not set a key. OpenBao *not
        answering* — an expired app token, the container down — means we do not know, and
        reporting `configured: false` there sends them hunting for a key they already set.
        That is precisely how #728 stayed misdiagnosed, so the distinction is now carried in
        the data. The core reports it; drawing it is the shell's job (ADR-0018) — the Models
        page's "Add a hosted model" row renders it inline (#922).
        """
        if secret_path is None:  # the local runtime holds no key at all
            return "not_required", None
        try:
            await self._secrets.get(secret_path, tenant)
        except SecretNotFoundError:
            return "missing", None
        except SecretError as exc:
            # Auth, network, a 403 that already names token expiry as the likely cause — all
            # of them "we could not ask", none of them "there is no key".
            log.warning("provider key state unknown", path=secret_path, error=str(exc))
            return "unavailable", str(exc)
        return "present", None

    async def models(
        self, tenant_id: str | None = None, *, with_capabilities: bool = False
    ) -> list[ModelInfo]:
        """List the local runtime's models, marking the ones loaded in memory or hidden.

        ``with_capabilities`` additionally fills each model's ``capabilities`` (e.g. ``tools``,
        ``vision``) and its trained ``context_length`` (#618) by querying ``/api/show`` per
        model, concurrently. It costs one extra call per model, so it is **opt-in** — the chat
        picker lists without it; the Models page asks for it to badge what each model can do
        and show its context window.
        """
        async with httpx.AsyncClient(base_url=self._ollama_url, timeout=10) as client:
            response = await client.get("/api/tags")
            response.raise_for_status()
            payload = response.json()
            loaded: set[str] = set()
            try:  # /api/ps lists running models; best-effort decoration only
                ps = await client.get("/api/ps")
                ps.raise_for_status()
                loaded = {m["name"] for m in ps.json().get("models", [])}
            except (httpx.HTTPError, KeyError):
                log.warning("ollama /api/ps failed; loaded-state unknown")
        hidden: set[str] = set()
        if self._prefs is not None:
            hidden = set(await self._prefs.get_hidden(tenant_id or self._default_tenant))
        infos = [
            ModelInfo(
                name=m["name"],
                size=m.get("size"),
                loaded=m["name"] in loaded,
                hidden=m["name"] in hidden,
            )
            for m in payload.get("models", [])
        ]
        if with_capabilities and infos:
            details = await asyncio.gather(*(self.show(info.name, tenant_id) for info in infos))
            for info, detail in zip(infos, details, strict=True):
                info.capabilities = detail.capabilities
                info.context_length = detail.context_length
        return infos

    async def _capabilities(self, model: str, tenant_id: str | None = None) -> list[str]:
        """The model's reported capabilities (best-effort; empty when unknown/unreported)."""
        return (await self.show(model, tenant_id)).capabilities

    async def supports_tools(self, model: str | None = None, tenant_id: str | None = None) -> bool:
        """Whether ``model`` can use tools — so the agent offers them only when they'll work.

        Passing tools to a model that cannot take them ends the turn: a local runtime errors,
        and a hosted provider whose server was started without tool-calling support returns a
        400 (#947). So the agent gates on this and falls back to a plain text answer, which the
        shell flags in the composer.

        Resolved by :meth:`show` — operator override, then what the gateway *learned* from the
        provider, then the catalogue (``/api/show`` for a local model, LiteLLM's map for a
        hosted one). Hosted is no longer assumed capable unconditionally, but an id the map has
        never heard of still answers **yes**: see :meth:`_hosted_details` for why that asymmetry
        with :meth:`supports_vision` is deliberate. ``None`` (the local runtime could not be
        asked at all) reads as yes, which is the pre-#711 behaviour for an unreported model.
        """
        resolved = model or await self.effective_default(tenant_id)
        answer = (await self.show(resolved, tenant_id)).supports_tools
        return True if answer is None else answer

    def _note_unmapped(self, litellm_model: str, message: str) -> None:
        """Report a LiteLLM cost-map miss once per model id per process, debug after (#711).

        A saved id outside that map is expected — the map is a curated static list, and the
        operator names the model (ADR-0010). Warning on every lookup made the box repeat the
        same line indefinitely; warning *once* still explains a model that shows no badges.
        """
        if litellm_model in self._unmapped_models:
            log.debug(message, model=litellm_model)
            return
        if len(self._unmapped_models) >= 512:
            self._unmapped_models.clear()  # bounded: ``model`` can arrive as a query param
        self._unmapped_models.add(litellm_model)
        log.warning(message, model=litellm_model)

    async def _capability_override(self, model: str, tenant_id: str | None) -> SavedModelOverride:
        """The operator's capability override for ``model`` (all-defaults when none) (#711).

        Never raises: a capability *hint* must not be able to break a capability *check*. A
        store hiccup degrades to the map's answer, which is exactly the pre-override behaviour.
        """
        if self._saved_models is None:
            return SavedModelOverride()
        try:
            return await self._saved_models.get_override(tenant_id or self._default_tenant, model)
        except Exception as exc:
            # Warn once, debug after. A cost-map miss is expected and stays quiet; this is not —
            # it silently drops *every* operator override back to the map's answer, which is the
            # same quiet capability failure #711 exists to fix. It has to be visible once.
            if self._override_store_failed:
                log.debug("capability override lookup failed", model=model, error=str(exc))
            else:
                self._override_store_failed = True
                log.warning("capability override lookup failed", model=model, error=str(exc))
            return SavedModelOverride()

    async def supports_vision(self, model: str | None = None, tenant_id: str | None = None) -> bool:
        """Whether ``model`` can take image input — gates an image attachment (#633).

        Deliberately stricter than :meth:`supports_tools` in two ways, because the failure
        mode is worse: offering tools to a model that can't use them degrades to a plain text
        answer, but sending an image to a model that can't see it either gets silently ignored
        or draws a provider 400 — the exact outcome this gate exists to prevent. So: (1) hosted
        providers are **not** assumed capable — LiteLLM already curates accurate per-model
        vision support (its cost/context map), so we ask it rather than guess; (2) a local
        model with no reported capabilities (older Ollama) defaults to **not** vision-capable —
        the opposite of ``supports_tools``'s "empty means don't restrict" — only an explicit
        ``vision`` entry says yes.

        Resolution order (#711): the operator's per-saved-model **override** first, then the
        source above. The map is authoritative until it is wrong — it omits ids entirely
        (``xai/grok-latest``) and mislabels others — and a curated static list being stale is
        not something the operator should have to work around by renaming their model.
        """
        resolved = model or await self.effective_default(tenant_id)
        override = await self._capability_override(resolved, tenant_id)
        if override.vision != "auto":
            return override.vision == "on"
        litellm_model, provider = registry.resolve(resolved)
        if provider.is_local:
            caps = await self._capabilities(resolved, tenant_id)
            return "vision" in caps
        try:
            return bool(litellm.supports_vision(model=litellm_model))
        except Exception:  # litellm raises a bare Exception for a model outside its cost map
            self._note_unmapped(litellm_model, "litellm supports_vision lookup failed")
            return False

    async def show(self, model: str, tenant_id: str | None = None) -> ModelDetails:
        """Read-only facts about ``model`` — from the runtime's ``/api/show`` when local, from
        LiteLLM's model-cost map when hosted (#633/#618).

        Local: returns empty details (all ``None``) rather than raising when the runtime is
        unreachable, so the model-settings sheet degrades to "unknown". The trained context
        length lives under ``model_info`` keyed by the architecture (e.g.
        ``llama.context_length``); fall back to any ``*.context_length`` if the arch is absent.

        Hosted: LiteLLM's cost/context map is the source of truth for both capabilities and
        context length — no provider call, and no fake default when the model isn't in the map
        — except where the tenant's saved-model **override** says otherwise (#711), which is
        why this takes a tenant (the overrides are tenant-scoped like every other stored row).
        """
        _, provider = registry.resolve(model)
        if not provider.is_local:
            return await self._hosted_details(model, tenant_id)
        try:
            async with httpx.AsyncClient(base_url=self._ollama_url, timeout=10) as client:
                response = await client.post("/api/show", json={"model": model})
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError):
            log.warning("ollama /api/show failed", model=model)
            return ModelDetails()
        details = payload.get("details") or {}
        info = payload.get("model_info") or {}
        arch = info.get("general.architecture")
        context_length: int | None = None
        arch_key = f"{arch}.context_length" if isinstance(arch, str) else None
        if arch_key and isinstance(info.get(arch_key), int):
            context_length = info[arch_key]
        else:
            context_length = next(
                (
                    value
                    for key, value in info.items()
                    if key.endswith(".context_length") and isinstance(value, int)
                ),
                None,
            )
        family = details.get("family")
        raw_caps = payload.get("capabilities")
        capabilities = (
            [c for c in raw_caps if isinstance(c, str)] if isinstance(raw_caps, list) else []
        )
        # The runtime names what the weights are for: an embedding model reports ``embedding``,
        # a chat model ``completion`` (plus ``tools``/``vision``/``thinking`` as it has them).
        # Anything else — including an older runtime that reports nothing — is ``unknown``, and
        # ``unknown`` is refused nothing (#944).
        role: ModelRole = (
            "embedding"
            if "embedding" in capabilities
            else "chat"
            if "completion" in capabilities
            else "unknown"
        )
        return ModelDetails(
            quantization=details.get("quantization_level") or None,
            parameter_size=details.get("parameter_size") or None,
            context_length=context_length,
            family=family if isinstance(family, str) else None,
            capabilities=capabilities,
            role=role,
            # An explicit list *without* ``tools`` is the only thing that disables them; an
            # empty list is an older runtime saying nothing, not saying no (#317).
            supports_tools=("tools" in capabilities) if capabilities else True,
        )

    async def _hosted_details(self, model: str, tenant_id: str | None = None) -> ModelDetails:
        """A hosted model's facts from LiteLLM's own model-cost/context map (#633/#618).

        No network call — this is a static lookup LiteLLM ships and updates independently.
        Empty/``None`` (never a fake default) when the model isn't in that map, e.g. a fresh
        or unlisted hosted id; ``in_catalogue`` now *says* so, instead of the miss being known
        only to a log line (#879).

        **Capability resolution (ADR-0140), in order, for each of the three questions:**

        1. the operator's per-saved-model **override** (#711) — authoritative, and it applies
           even when the map lookup fails outright, since an id absent from the map is exactly
           the case the override exists for;
        2. what the gateway **learned** from the provider — only ``tools``, only ever a "no",
           and only from a real rejection (#947);
        3. the **catalogue** (LiteLLM's map): ``mode`` for the role, ``supports_vision``,
           ``supports_function_calling``.

        The defaults for a model the map has never heard of are deliberately **asymmetric**.
        Vision answers *no*: sending an image to a model that cannot see it is silently ignored
        or draws a 400, which is what the gate exists to prevent, and an unlisted id is a poor
        reason to try. Tools answer *yes*: the map is thin, most hosted models do call tools,
        and a wrong yes now self-heals — the provider's refusal is learned and the same turn
        retries without tools — whereas a wrong no would quietly strip every module from the
        assistant. Both wrong answers are visible in the shell and one click from being fixed.
        """
        override = await self._capability_override(model, tenant_id)
        litellm_model, _ = registry.resolve(model)
        info: Any = {}
        in_catalogue = True
        try:
            info = litellm.get_model_info(model=litellm_model)
        except Exception:  # litellm raises a bare Exception for a model outside its cost map
            in_catalogue = False
            self._note_unmapped(litellm_model, "litellm get_model_info lookup failed")
        mapped_context = info.get("max_input_tokens") or info.get("max_tokens")
        context_length = override.context_length or (
            mapped_context if isinstance(mapped_context, int) else None
        )
        has_vision = (
            bool(info.get("supports_vision"))
            if override.vision == "auto"
            else override.vision == "on"
        )
        role: ModelRole = (
            _role_from_mode(info.get("mode")) if override.role == "auto" else override.role
        )
        if override.tools != "auto":
            has_tools = override.tools == "on"
        elif override.tools_learned == "off":
            has_tools = False
        else:
            has_tools = bool(info.get("supports_function_calling")) if in_catalogue else True
        # An embedding model's badges are about embedding: it has neither tools nor vision, and
        # saying so is what lets the Models page show the operator why a chat with it failed.
        capabilities = ["embedding"] if role == "embedding" else []
        if role != "embedding":
            if has_tools:
                capabilities.append("tools")
            if has_vision:
                capabilities.append("vision")
        return ModelDetails(
            context_length=context_length,
            capabilities=capabilities,
            role=role,
            supports_tools=False if role == "embedding" else has_tools,
            in_catalogue=in_catalogue,
        )

    def _forget_local_role(self, model: str) -> None:
        """Drop a memoised local role — the weights behind the name may have changed (#944)."""
        self._local_roles.pop(model, None)

    async def pull(self, model: str) -> None:
        """Pull a model into the local runtime (blocks until complete)."""
        self._forget_local_role(model)
        async with httpx.AsyncClient(base_url=self._ollama_url, timeout=None) as client:
            response = await client.post("/api/pull", json={"model": model, "stream": False})
            response.raise_for_status()

    async def pull_stream(self, model: str) -> AsyncIterator[dict[str, Any]]:
        """Pull a model, yielding the runtime's progress objects as they arrive.

        Each item is Ollama's progress shape (``status``, and ``total``/``completed``
        while a layer downloads) — the model-manager UI renders these directly.
        """
        self._forget_local_role(model)
        async with (
            httpx.AsyncClient(base_url=self._ollama_url, timeout=None) as client,
            client.stream("POST", "/api/pull", json={"model": model, "stream": True}) as response,
        ):
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.strip():
                    item: dict[str, Any] = json.loads(line)
                    yield item

    async def delete_model(self, model: str) -> None:
        """Remove a model from the local runtime."""
        self._forget_local_role(model)
        async with httpx.AsyncClient(base_url=self._ollama_url, timeout=30) as client:
            response = await client.request("DELETE", "/api/delete", json={"model": model})
            response.raise_for_status()

    async def unload(self, model: str | None = None) -> None:
        """Best-effort: ask the runtime to drop loaded models now (``keep_alive=0``).

        With ``model`` set, unload just that one (the on-demand per-model Unload, #331);
        otherwise unload every installed model (the power-pause path). Never raises — a
        runtime hiccup is logged, not surfaced.
        """
        try:
            targets = [model] if model is not None else [info.name for info in await self.models()]
            async with httpx.AsyncClient(base_url=self._ollama_url, timeout=10) as client:
                for name in targets:
                    await client.post("/api/generate", json={"model": name, "keep_alive": 0})
        except (httpx.HTTPError, KeyError):
            log.warning("ollama unload failed", model=model, exc_info=True)
