"""Provider registry — maps epicurus model aliases to LiteLLM + the OpenBao key path.

A model string is ``<provider>/<model>`` (e.g. ``claude/claude-3-5-sonnet-latest``); a
bare name (no ``/``) targets the local Ollama runtime. Model IDs are the caller's
choice (config, not code) — only the provider set is fixed here (ADR-0010).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    """How to reach a provider through LiteLLM."""

    litellm_prefix: str
    # OpenBao path holding the key (``{"api_key": ...}``); ``None`` for the local runtime.
    secret_path: str | None = None
    # Generic OpenAI-compatible providers also read ``api_base`` from the secret.
    needs_base_url: bool = False

    @property
    def is_local(self) -> bool:
        return self.secret_path is None


# Epicurus alias -> provider. Aliases are deliberately friendlier than LiteLLM's
# prefixes. "custom" is the generic OpenAI-compatible escape hatch (the "any LLM" path).
PROVIDERS: dict[str, Provider] = {
    "local": Provider("ollama_chat"),
    "claude": Provider("anthropic", "llm/anthropic"),
    "gpt": Provider("openai", "llm/openai"),
    "grok": Provider("xai", "llm/xai"),
    "deepseek": Provider("deepseek", "llm/deepseek"),
    "gemini": Provider("gemini", "llm/google"),
    "custom": Provider("openai", "llm/custom", needs_base_url=True),
}


def resolve(model: str) -> tuple[str, Provider]:
    """Resolve a model string to ``(litellm_model, provider)``.

    ``<alias>/<model>`` routes to that provider; a bare name (or any unknown prefix)
    targets the local Ollama runtime.
    """
    alias, sep, rest = model.partition("/")
    if sep and alias in PROVIDERS:
        provider = PROVIDERS[alias]
        return f"{provider.litellm_prefix}/{rest}", provider
    local = PROVIDERS["local"]
    return f"{local.litellm_prefix}/{model}", local
