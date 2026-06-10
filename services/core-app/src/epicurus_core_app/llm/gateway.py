"""The LLM gateway — the core's single entry point to language models (ADR-0010).

Targets the local Ollama runtime plus hosted providers (Claude, ChatGPT, Grok,
DeepSeek, Gemini, and a generic OpenAI-compatible escape hatch) through the LiteLLM
SDK. Provider keys are fetched from OpenBao at call time (tenant-scoped) and never
logged. Model list / pull use Ollama's native API. LiteLLM telemetry is disabled
(local-first); params a provider does not support are dropped rather than raising.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import litellm

from epicurus_core import SecretError, SecretStore, get_logger
from epicurus_core_app.llm import providers as registry
from epicurus_core_app.llm.models import ChatMessage, ChatResult, ModelInfo, ProviderInfo
from epicurus_core_app.llm.power import GatewayPausedError, PowerController

litellm.telemetry = False
litellm.drop_params = True

log = get_logger("epicurus_core_app.llm")


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
    ) -> None:
        self._ollama_url = ollama_url.rstrip("/")
        self._default_model = default_model
        self._keep_alive = keep_alive
        self._power = power
        self._secrets = secrets
        self._default_tenant = default_tenant

    async def _call_config(self, model: str | None, tenant_id: str | None) -> dict[str, Any]:
        """The LiteLLM call kwargs (model, endpoint, key) for ``model``.

        For hosted providers the API key is fetched from OpenBao at call time and is
        never logged.
        """
        litellm_model, provider = registry.resolve(model or self._default_model)
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

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        tenant_id: str | None = None,
    ) -> ChatResult:
        """Return a single completion for ``messages``."""
        self._guard()
        config = await self._call_config(model, tenant_id)
        response = await litellm.acompletion(
            messages=[m.model_dump() for m in messages],
            tools=tools,
            **config,
        )
        self._power.mark_active()
        data: dict[str, Any] = response.model_dump()
        message = data["choices"][0]["message"]
        usage = data.get("usage") or {}
        return ChatResult(
            model=data.get("model") or config["model"],
            content=message.get("content") or "",
            tool_calls=message.get("tool_calls"),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        tenant_id: str | None = None,
    ) -> AsyncIterator[str]:
        """Yield content deltas as the model produces them."""
        self._guard()
        config = await self._call_config(model, tenant_id)
        response = await litellm.acompletion(
            messages=[m.model_dump() for m in messages],
            stream=True,
            **config,
        )
        self._power.mark_active()
        async for chunk in response:
            choices = chunk.choices
            if choices and (piece := choices[0].delta.content):
                yield piece

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        """Embed ``texts`` with a local embedding model (e.g. ``nomic-embed-text``)."""
        self._guard()
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

    def _guard(self) -> None:
        if self._power.paused:
            raise GatewayPausedError("LLM gateway is paused; resume to run inference")
