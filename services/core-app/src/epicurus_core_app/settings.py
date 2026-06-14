"""Settings for the core runtime — CoreSettings plus the core-only LLM-gateway knobs."""

from __future__ import annotations

from pydantic import field_validator

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
    # ── LLM tuning (optional; None = use the provider/runtime default) ──────────
    # These pass straight through to the LiteLLM call, so tuning needs no code
    # edit — set the env var and restart (extend this block to wire more knobs).
    # Sampling temperature, applied to each chat completion (local and hosted).
    llm_temperature: float | None = None
    # Nucleus-sampling top_p, applied to each chat completion (local and hosted).
    llm_top_p: float | None = None
    # Ollama context-window size (num_ctx); applied to local models only.
    llm_num_ctx: int | None = None
    # Comma-separated module base URLs. Each module serves its MCP tools at
    # <base>/mcp (the agent calls these) and its manifest at <base>/manifest
    # (the registry + web shell read these).
    module_urls: str = (
        "http://echo:8080,http://storage:8080,http://knowledge:8080,"
        "http://websearch:8080,http://calendar:8080,http://mail:8080,http://tasks:8080"
    )
    # Max tool-calling rounds in one agent turn before it must answer.
    agent_max_steps: int = 4
    # Base URL of the module that durably keeps chat uploads (ADR-0025). The attachment
    # upload route best-effort POSTs each uploaded file's bytes to <url>/ingest so it
    # becomes browsable in the Files page. Empty disables the sink (e.g. an OSS build
    # without the storage module); a failed push never breaks the upload.
    attachment_sink_url: str = "http://storage:8080"
    # Postgres DSN (async driver) for conversation persistence.
    database_url: str = "postgresql+asyncpg://epicurus:epicurus-dev@localhost:5432/epicurus"
    # Qdrant endpoint for semantic recall.
    qdrant_url: str = "http://localhost:6333"
    # Ollama embedding model used to vectorize conversation text for recall.
    memory_embed_model: str = "nomic-embed-text"

    # ── OAuth settings ────────────────────────────────────────────────────────
    # Public base URL of the server used to build the OAuth redirect_uri.
    # Must exactly match the URI registered with each OAuth provider.
    # Example: http://localhost:8084 (local web port), https://epicurus.example.com
    oauth_redirect_base_url: str = "http://localhost:8084"
    # HMAC key for signing the OAuth ``state`` parameter (CSRF protection).
    # Change this before first use; rotating it invalidates in-flight connect flows.
    oauth_state_secret: str = "change-this-before-use"

    @field_validator("llm_temperature", "llm_top_p", "llm_num_ctx", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        """Treat a blank env value (e.g. ``LLM_TEMPERATURE=``) as unset.

        Compose passes these as ``${LLM_TEMPERATURE:-}``; an empty string would
        otherwise fail numeric parsing instead of falling back to the default.
        """
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @property
    def fallback_models(self) -> list[str]:
        """The fallback chain parsed from ``llm_fallbacks``."""
        return [m.strip() for m in self.llm_fallbacks.split(",") if m.strip()]

    @property
    def module_base_urls(self) -> list[str]:
        """The module base URLs parsed from ``module_urls``."""
        return [u.strip().rstrip("/") for u in self.module_urls.split(",") if u.strip()]

    @property
    def module_mcp_urls(self) -> list[str]:
        """Each module's MCP endpoint (``<base>/mcp``)."""
        return [f"{base}/mcp" for base in self.module_base_urls]
