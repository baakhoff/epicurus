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
