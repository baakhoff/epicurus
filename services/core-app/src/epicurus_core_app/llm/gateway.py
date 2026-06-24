"""The LLM gateway — the core's single entry point to language models (ADR-0010).

Targets the local Ollama runtime plus hosted providers (Claude, ChatGPT, Grok,
DeepSeek, Gemini, and a generic OpenAI-compatible escape hatch) through the LiteLLM
SDK. Provider keys are fetched from OpenBao at call time (tenant-scoped) and never
logged.

Routing (ADR-0010): a request tries the chosen model, then the configured fallback
chain on failure. While the runtime is **paused** (ADR-0005), local models are
skipped — running one would wake the GPU — but hosted providers stay available, so a
hosted fallback still serves. Each call emits a usage event on NATS (no prompt
content, no keys). Retries on 429/5xx use LiteLLM's exponential backoff.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import litellm

from epicurus_core import EventBus, SecretError, SecretStore, get_logger
from epicurus_core_app.llm import providers as registry
from epicurus_core_app.llm.compaction import (
    compact_messages,
    estimate_tools_tokens,
    reply_reserve,
)
from epicurus_core_app.llm.model_settings import ModelSettings, ModelSettingsStore
from epicurus_core_app.llm.models import (
    ChatMessage,
    ChatResult,
    ModelDetails,
    ModelInfo,
    ProviderInfo,
    StreamEvent,
    UsageEvent,
)
from epicurus_core_app.llm.power import GatewayPausedError, PowerController
from epicurus_core_app.llm.prefs import LlmPrefsStore
from epicurus_core_app.llm.reasoning import ThinkSplitter, split_reasoning

litellm.telemetry = False
litellm.drop_params = True

log = get_logger("epicurus_core_app.llm")

USAGE_SUBJECT = "llm.usage"

# Inserted in place of dropped history when a turn is trimmed to fit the context window, so the
# model knows earlier messages were cut rather than never said.
_TRIM_NOTE = "(Earlier messages in this conversation were trimmed to fit the context window.)"


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
        temperature: float | None = None,
        top_p: float | None = None,
        num_ctx: int | None = None,
        prefs: LlmPrefsStore | None = None,
        model_settings: ModelSettingsStore | None = None,
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
        self._temperature = temperature
        self._top_p = top_p
        self._num_ctx = num_ctx
        self._prefs = prefs
        self._model_settings = model_settings

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

    async def _fit_to_context(
        self,
        model: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None,
        tenant_id: str | None,
    ) -> list[ChatMessage]:
        """Trim ``messages`` to fit ``model``'s context window — local models only.

        The local runtime silently drops tokens past ``num_ctx``, evicting the oldest (the
        system prompt + recalled context). We pre-trim instead (see :mod:`compaction`): keep the
        system prefix and the most-recent turns within ``num_ctx`` minus a reply reserve and the
        tool schemas' footprint. Hosted providers (large contexts, handled server-side) and calls
        with no known window are left untouched.
        """
        _, provider = registry.resolve(model)
        if not provider.is_local:
            return messages
        num_ctx = await self._effective_num_ctx(model, tenant_id)
        if not num_ctx:
            return messages
        budget = num_ctx - reply_reserve(num_ctx) - estimate_tools_tokens(tools)
        return compact_messages(messages, budget=budget, note=_TRIM_NOTE)

    async def _complete(
        self,
        model: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None,
        tenant_id: str | None,
    ) -> ChatResult:
        config = await self._call_config(model, tenant_id)
        messages = await self._fit_to_context(model, messages, tools, tenant_id)
        start = time.monotonic()
        response = await litellm.acompletion(
            messages=[m.provider_dump() for m in messages],
            tools=tools,
            num_retries=self._num_retries,
            **config,
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
            tool_calls=message.get("tool_calls"),
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
        )
        return result

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        tenant_id: str | None = None,
    ) -> ChatResult:
        """Return a completion, walking the fallback chain on failure."""
        resolved = model or await self.effective_default(tenant_id)
        last_error: Exception | None = None
        for candidate in self._candidates(resolved):
            if not self._is_available(candidate):
                continue
            try:
                return await self._complete(candidate, messages, tools, tenant_id)
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
        candidate = next((c for c in self._candidates(resolved) if self._is_available(c)), None)
        if candidate is None:
            raise GatewayPausedError("LLM gateway is paused; no non-local model is available")
        config = await self._call_config(candidate, tenant_id)
        messages = await self._fit_to_context(candidate, messages, None, tenant_id)
        start = time.monotonic()
        response = await litellm.acompletion(
            messages=[m.provider_dump() for m in messages],
            stream=True,
            num_retries=self._num_retries,
            **config,
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
        ``result.tool_calls`` is complete — the agent loop streams every round.
        Uses the first available candidate (no mid-stream fallback).
        """
        resolved = model or await self.effective_default(tenant_id)
        candidate = next((c for c in self._candidates(resolved) if self._is_available(c)), None)
        if candidate is None:
            raise GatewayPausedError("LLM gateway is paused; no non-local model is available")
        config = await self._call_config(candidate, tenant_id)
        messages = await self._fit_to_context(candidate, messages, tools, tenant_id)
        start = time.monotonic()
        response = await litellm.acompletion(
            messages=[m.provider_dump() for m in messages],
            tools=tools,
            stream=True,
            num_retries=self._num_retries,
            **config,
        )
        self._power.mark_active()
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        splitter = ThinkSplitter()
        calls: dict[int, dict[str, Any]] = {}
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
                index = fragment.index or 0
                entry = calls.setdefault(
                    index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                )
                if fragment.id:
                    entry["id"] = fragment.id
                function = getattr(fragment, "function", None)
                if function is None:
                    continue
                if function.name:
                    entry["function"]["name"] = function.name
                arguments = function.arguments
                if isinstance(arguments, str):
                    entry["function"]["arguments"] += arguments
                elif arguments is not None:  # some providers send whole args as a dict
                    entry["function"]["arguments"] = arguments
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
            tool_calls=[calls[i] for i in sorted(calls)] or None,
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
    ) -> None:
        """Publish a usage event on NATS. Best-effort — never breaks inference."""
        tenant = tenant_id or self._default_tenant
        event = UsageEvent(
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=round(latency_ms),
            tenant=tenant,
        )
        try:
            await self._bus.publish(USAGE_SUBJECT, event.model_dump(), tenant_id=tenant)
        except Exception:  # usage accounting must never break inference
            log.warning("usage event publish failed", exc_info=True)

    async def embed(
        self, texts: list[str], *, model: str | None = None, tenant_id: str | None = None
    ) -> list[list[float]]:
        """Embed ``texts`` with a local embedding model (e.g. ``nomic-embed-text``).

        The embedding model gets the same per-model settings sheet as a chat model: when the
        operator has set a context window or keep-alive for it, those are passed as Ollama
        runtime options (LiteLLM drops them if the runtime doesn't take them). With nothing
        set, the call is unchanged — embeddings stay opt-in, never silently retuned.
        """
        if self._power.paused:
            raise GatewayPausedError("LLM gateway is paused; resume to run inference")
        resolved = model or await self.effective_embed_default(tenant_id)
        embed_model = f"ollama/{resolved}"
        settings = await self._settings_for(resolved, tenant_id)
        options: dict[str, Any] = {}
        if settings.context_window is not None:
            options["num_ctx"] = settings.context_window
        if settings.keep_alive:
            options["keep_alive"] = settings.keep_alive
        if settings.device == "cpu":
            options["num_gpu"] = 0
        elif settings.device == "gpu":
            options["num_gpu"] = 999
        start = time.monotonic()
        response = await litellm.aembedding(
            model=embed_model,
            input=texts,
            api_base=self._ollama_url,
            **options,
        )
        self._power.mark_active()
        await self._emit_usage(
            model=embed_model,
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
        """List the providers and whether each one's key is present in OpenBao."""
        tenant = tenant_id or self._default_tenant
        infos: list[ProviderInfo] = []
        for alias, provider in registry.PROVIDERS.items():
            configured = provider.is_local or await self._key_present(provider.secret_path, tenant)
            infos.append(
                ProviderInfo(
                    alias=alias,
                    local=provider.is_local,
                    configured=configured,
                    needs_base_url=provider.needs_base_url,
                )
            )
        return infos

    async def _key_present(self, secret_path: str | None, tenant: str) -> bool:
        if secret_path is None:
            return True
        try:
            await self._secrets.get(secret_path, tenant)
        except SecretError:
            return False
        return True

    async def models(self, tenant_id: str | None = None) -> list[ModelInfo]:
        """List the local runtime's models, marking the ones loaded in memory or hidden."""
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
        return [
            ModelInfo(
                name=m["name"],
                size=m.get("size"),
                loaded=m["name"] in loaded,
                hidden=m["name"] in hidden,
            )
            for m in payload.get("models", [])
        ]

    async def show(self, model: str) -> ModelDetails:
        """Read-only facts about a local model from the runtime's ``/api/show``.

        Returns empty details (all ``None``) rather than raising when the model isn't local or
        the runtime is unreachable, so the model-settings sheet degrades to "unknown". The
        trained context length lives under ``model_info`` keyed by the architecture (e.g.
        ``llama.context_length``); fall back to any ``*.context_length`` if the arch is absent.
        """
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
        return ModelDetails(
            quantization=details.get("quantization_level") or None,
            parameter_size=details.get("parameter_size") or None,
            context_length=context_length,
            family=family if isinstance(family, str) else None,
        )

    async def pull(self, model: str) -> None:
        """Pull a model into the local runtime (blocks until complete)."""
        async with httpx.AsyncClient(base_url=self._ollama_url, timeout=None) as client:
            response = await client.post("/api/pull", json={"model": model, "stream": False})
            response.raise_for_status()

    async def pull_stream(self, model: str) -> AsyncIterator[dict[str, Any]]:
        """Pull a model, yielding the runtime's progress objects as they arrive.

        Each item is Ollama's progress shape (``status``, and ``total``/``completed``
        while a layer downloads) — the model-manager UI renders these directly.
        """
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
        async with httpx.AsyncClient(base_url=self._ollama_url, timeout=30) as client:
            response = await client.request("DELETE", "/api/delete", json={"model": model})
            response.raise_for_status()

    async def unload(self) -> None:
        """Best-effort: ask the runtime to drop loaded models now (``keep_alive=0``)."""
        try:
            models = await self.models()
            async with httpx.AsyncClient(base_url=self._ollama_url, timeout=10) as client:
                for info in models:
                    await client.post("/api/generate", json={"model": info.name, "keep_alive": 0})
        except (httpx.HTTPError, KeyError):
            log.warning("ollama unload failed", exc_info=True)
