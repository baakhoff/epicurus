"""The LLM gateway — the core's single entry point to language models (ADR-0010).

v1 targets a local Ollama runtime: chat / stream / embed via the LiteLLM SDK, and
model list / pull via Ollama's native API. Hosted providers and routing land with
#36 / #37. LiteLLM telemetry is disabled (local-first) and params a provider does not
support are dropped rather than raising.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import litellm

from epicurus_core import get_logger
from epicurus_core_app.llm.models import ChatMessage, ChatResult, ModelInfo
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
    ) -> None:
        self._ollama_url = ollama_url.rstrip("/")
        self._default_model = default_model
        self._keep_alive = keep_alive
        self._power = power

    def _chat_model(self, model: str | None) -> str:
        return f"ollama_chat/{model or self._default_model}"

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResult:
        """Return a single completion for ``messages``."""
        self._guard()
        response = await litellm.acompletion(
            model=self._chat_model(model),
            messages=[m.model_dump() for m in messages],
            tools=tools,
            api_base=self._ollama_url,
            keep_alive=self._keep_alive,
        )
        self._power.mark_active()
        data: dict[str, Any] = response.model_dump()
        message = data["choices"][0]["message"]
        usage = data.get("usage") or {}
        return ChatResult(
            model=data.get("model") or self._chat_model(model),
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
    ) -> AsyncIterator[str]:
        """Yield content deltas as the model produces them."""
        self._guard()
        response = await litellm.acompletion(
            model=self._chat_model(model),
            messages=[m.model_dump() for m in messages],
            api_base=self._ollama_url,
            keep_alive=self._keep_alive,
            stream=True,
        )
        self._power.mark_active()
        async for chunk in response:
            choices = chunk.choices
            if choices and (piece := choices[0].delta.content):
                yield piece

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        """Embed ``texts`` (needs an embedding model, e.g. ``nomic-embed-text``)."""
        self._guard()
        response = await litellm.aembedding(
            model=f"ollama/{model or self._default_model}",
            input=texts,
            api_base=self._ollama_url,
        )
        self._power.mark_active()
        data: dict[str, Any] = response.model_dump()
        return [item["embedding"] for item in data["data"]]

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
