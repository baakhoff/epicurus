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

import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import litellm

from epicurus_core import EventBus, SecretError, SecretStore, get_logger
from epicurus_core_app.llm import providers as registry
from epicurus_core_app.llm.models import (
    ChatMessage,
    ChatResult,
    ModelInfo,
    ProviderInfo,
    UsageEvent,
)
from epicurus_core_app.llm.power import GatewayPausedError, PowerController

litellm.telemetry = False
litellm.drop_params = True

log = get_logger("epicurus_core_app.llm")

USAGE_SUBJECT = "llm.usage"


class LlmGateway:
    """Unified, provider-agnostic access to language models."""

    def __init__(
        self,
        *,
        ollama_url: str,
        default_model: str,
        keep_alive: str,
        power: PowerController,
        secrets: SecretStore,
        default_tenant: str,
        bus: EventBus,
        fallbacks: list[str],
        num_retries: int = 2,
    ) -> None:
        self._ollama_url = ollama_url.rstrip("/")
        self._default_model = default_model
        self._keep_alive = keep_alive
        self._power = power
        self._secrets = secrets
        self._default_tenant = default_tenant
        self._bus = bus
        self._fallbacks = list(fallbacks)
        self._num_retries = num_retries

    def _candidates(self, model: str | None) -> list[str]:
        """The chosen model followed by the configured fallback chain (deduped)."""
        ordered = [model or self._default_model]
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
        """The LiteLLM call kwargs (model, endpoint, key) for ``model``.

        For hosted providers the API key is fetched from OpenBao at call time and is
        never logged.
        """
        litellm_model, provider = registry.resolve(model)
        config: dict[str, Any] = {"model": litellm_model}
        if provider.is_local:
            config["api_base"] = self._ollama_url
            config["keep_alive"] = self._keep_alive
        if provider.secret_path is not None:
            tenant = tenant_id or self._default_tenant
            secret = await self._secrets.get(provider.secret_path, tenant)
            config["api_key"] = secret["api_key"]
            if provider.needs_base_url:
                config["api_base"] = secret["api_base"]
        return config

    async def _complete(
        self,
        model: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None,
        tenant_id: str | None,
    ) -> ChatResult:
        config = await self._call_config(model, tenant_id)
        start = time.monotonic()
        response = await litellm.acompletion(
            messages=[m.model_dump(exclude_none=True) for m in messages],
            tools=tools,
            num_retries=self._num_retries,
            **config,
        )
        latency_ms = (time.monotonic() - start) * 1000
        self._power.mark_active()
        data: dict[str, Any] = response.model_dump()
        message = data["choices"][0]["message"]
        usage = data.get("usage") or {}
        result = ChatResult(
            model=data.get("model") or config["model"],
            content=message.get("content") or "",
            tool_calls=message.get("tool_calls"),
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
        last_error: Exception | None = None
        for candidate in self._candidates(model):
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
        candidate = next((c for c in self._candidates(model) if self._is_available(c)), None)
        if candidate is None:
            raise GatewayPausedError("LLM gateway is paused; no non-local model is available")
        config = await self._call_config(candidate, tenant_id)
        start = time.monotonic()
        response = await litellm.acompletion(
            messages=[m.model_dump(exclude_none=True) for m in messages],
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

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        """Embed ``texts`` with a local embedding model (e.g. ``nomic-embed-text``)."""
        if self._power.paused:
            raise GatewayPausedError("LLM gateway is paused; resume to run inference")
        response = await litellm.aembedding(
            model=f"ollama/{model or self._default_model}",
            input=texts,
            api_base=self._ollama_url,
        )
        self._power.mark_active()
        data: dict[str, Any] = response.model_dump()
        return [item["embedding"] for item in data["data"]]

    async def providers(self, tenant_id: str | None = None) -> list[ProviderInfo]:
        """List the providers and whether each one's key is present in OpenBao."""
        tenant = tenant_id or self._default_tenant
        infos: list[ProviderInfo] = []
        for alias, provider in registry.PROVIDERS.items():
            configured = provider.is_local or await self._key_present(provider.secret_path, tenant)
            infos.append(ProviderInfo(alias=alias, local=provider.is_local, configured=configured))
        return infos

    async def _key_present(self, secret_path: str | None, tenant: str) -> bool:
        if secret_path is None:
            return True
        try:
            await self._secrets.get(secret_path, tenant)
        except SecretError:
            return False
        return True

    async def models(self) -> list[ModelInfo]:
        """List the models available in the local runtime."""
        async with httpx.AsyncClient(base_url=self._ollama_url, timeout=10) as client:
            response = await client.get("/api/tags")
            response.raise_for_status()
            payload = response.json()
        return [ModelInfo(name=m["name"], size=m.get("size")) for m in payload.get("models", [])]

    async def pull(self, model: str) -> None:
        """Pull a model into the local runtime (blocks until complete)."""
        async with httpx.AsyncClient(base_url=self._ollama_url, timeout=None) as client:
            response = await client.post("/api/pull", json={"model": model, "stream": False})
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
