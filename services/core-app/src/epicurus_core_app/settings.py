"""Settings for the core runtime — CoreSettings plus the core-only LLM-gateway knobs."""

from __future__ import annotations

from epicurus_core import CoreSettings


class CoreAppSettings(CoreSettings):
    """Adds the LLM-gateway configuration to the shared settings."""

    # Ollama, the local LLM runtime. On the internal Docker network: http://ollama:11434.
    ollama_url: str = "http://localhost:11434"
    # Default Ollama model used when a request does not name one.
    llm_default_model: str = "llama3.2"
    # How long Ollama keeps a model loaded after its last use (idle unload, ADR-0005).
    llm_keep_alive: str = "5m"
    # Comma-separated fallback models, tried in order when the primary fails or is
    # unavailable (e.g. "claude/claude-3-5-sonnet-latest,gpt/gpt-4o").
    llm_fallbacks: str = ""
    # Per-model retries on 429 / 5xx (exponential backoff), handled by LiteLLM.
    llm_num_retries: int = 2
    # Comma-separated module MCP endpoints the agent discovers + calls tools from.
    mcp_module_urls: str = "http://echo:8080/mcp"
    # Max tool-calling rounds in one agent turn before it must answer.
    agent_max_steps: int = 4

    @property
    def fallback_models(self) -> list[str]:
        """The fallback chain parsed from ``llm_fallbacks``."""
        return [m.strip() for m in self.llm_fallbacks.split(",") if m.strip()]

    @property
    def module_mcp_urls(self) -> list[str]:
        """The module MCP endpoints parsed from ``mcp_module_urls``."""
        return [u.strip() for u in self.mcp_module_urls.split(",") if u.strip()]
