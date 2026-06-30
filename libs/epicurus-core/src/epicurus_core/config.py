"""Application configuration shared by every epicurus service.

Values load from environment variables (and an optional local ``.env``). Secrets
do NOT belong here — they come from OpenBao at runtime. This is for non-secret,
machine-local configuration only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from epicurus_core.tenancy import is_valid_tenant_id

Environment = Literal["local", "staging", "production"]
LogLevel = Literal["debug", "info", "warning", "error", "critical"]


class CoreSettings(BaseSettings):
    """Settings every service shares. Subclass to add service-specific fields."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Identity of the running service; override per service.
    service_name: str = "epicurus"

    # Deployment environment.
    app_env: Environment = "local"

    # Logging verbosity.
    log_level: LogLevel = "info"

    # Force JSON logs on/off. When None, decided by environment.
    json_logs: bool | None = None

    # Default tenant for single-tenant / self-host. Multi-tenant SaaS resolves the
    # tenant per request instead (see the tenancy module).
    default_tenant_id: str = "local"

    # NATS event backbone. On the internal Docker network this is nats://nats:4222;
    # the contract is local-only.
    nats_url: str = "nats://localhost:4222"

    # OpenBao (secrets). On the internal Docker network the address is
    # http://openbao:8200. The token is the bootstrap secret, injected at runtime
    # — directly via OPENBAO_TOKEN, or via OPENBAO_TOKEN_FILE pointing at a
    # mounted file (e.g. a Docker secret) — and never committed.
    openbao_url: str = "http://localhost:8200"
    openbao_token: str | None = None
    openbao_token_file: str | None = None

    # OpenTelemetry tracing (epicurus_core.tracing, #57). Off by default — the lean
    # stack pays nothing and disabled tracing is a runtime no-op. Set
    # OTEL_TRACES_ENABLED=true (with the `observability` profile up, so Tempo is
    # listening) to emit spans fleet-wide. The endpoint is the OTLP/HTTP base — the
    # exporter appends `/v1/traces` — pointing at Tempo on the internal Docker network.
    otel_traces_enabled: bool = False
    otel_exporter_otlp_endpoint: str = "http://tempo:4318"

    @field_validator("default_tenant_id")
    @classmethod
    def _validate_default_tenant(cls, value: str) -> str:
        if not is_valid_tenant_id(value):
            raise ValueError(
                f"invalid default_tenant_id {value!r}: must be lowercase "
                "alphanumeric and hyphens (1-63 chars, no leading/trailing hyphen)"
            )
        return value

    def resolve_openbao_token(self) -> str | None:
        """The OpenBao bootstrap token: the explicit value, else the token file's
        content (stripped), else ``None``."""
        if self.openbao_token is not None:
            return self.openbao_token
        if self.openbao_token_file is not None:
            return Path(self.openbao_token_file).read_text(encoding="utf-8").strip()
        return None

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def use_json_logs(self) -> bool:
        """Whether to render logs as JSON, honoring the explicit override."""
        if self.json_logs is not None:
            return self.json_logs
        return self.app_env != "local"
