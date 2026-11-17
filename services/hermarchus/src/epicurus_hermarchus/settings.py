"""Hermarchus configuration — environment variables prefixed ``HERMARCHUS_``.

Everything here is machine-local and non-secret except the bearer token, which is read
from ``HERMARCHUS_TOKEN`` or a mounted file (``HERMARCHUS_TOKEN_FILE``) and is never handed
to an agent process (see :mod:`epicurus_hermarchus.sandbox`). ``LOG_LEVEL`` / ``APP_ENV`` /
``JSON_LOGS`` are the stack-wide unprefixed names every service reads.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from epicurus_core.tenancy import TenantError, validate_tenant_id

__all__ = ["MIN_TOKEN_LENGTH", "HermarchusSettings", "SettingsError"]

# A token shorter than this is refused at startup: it is the only thing between the network
# and a program that can write the workspace and run commands.
MIN_TOKEN_LENGTH = 16


class SettingsError(RuntimeError):
    """The configuration cannot be served; the message names the fix."""


def _csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


class HermarchusSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HERMARCHUS_", extra="ignore")

    # ── identity and access ────────────────────────────────────────────────
    token: SecretStr | None = None
    token_file: Path | None = None
    # The one profile (tenant id) this instance is bound to.
    profile: str = "local"
    port: int = 8080

    # ── filesystem ─────────────────────────────────────────────────────────
    # The agents' workspace: every session's working directory resolves inside it.
    workspace_dir: Path = Path("/workspace")
    # The SQLite spool (sessions, events, inbox, remembered permissions).
    state_dir: Path = Path("/state")
    # HOME for agent processes: installed agent programs and their sign-ins live here.
    agent_home: Path = Path("/home/hermarchus")

    # ── limits ─────────────────────────────────────────────────────────────
    max_sessions: int = Field(default=10, ge=1)
    # Events buffered per streaming connection before that connection is dropped.
    stream_queue_size: int = Field(default=1000, ge=1)
    # How long an attended permission request may wait for an answer before it is
    # rejected. Nothing ever waits forever.
    permission_timeout_s: float = Field(default=1800.0, gt=0)
    # Unattended sessions reject anything nobody answers within this much shorter window.
    unattended: bool = False
    unattended_timeout_s: float = Field(default=60.0, gt=0)
    # initialize + session/new must finish within this.
    agent_start_timeout_s: float = Field(default=120.0, gt=0)
    # How long a stopping agent gets after SIGTERM before SIGKILL.
    stop_grace_s: float = Field(default=5.0, ge=0)

    # ── agent kinds ────────────────────────────────────────────────────────
    # A YAML file of extra / overriding kinds (see kinds.py for the shape).
    kinds_file: Path | None = None
    # Kinds whose `install` command runs at start (comma-separated). Never fatal.
    install_kinds: str = ""
    install_timeout_s: float = Field(default=900.0, gt=0)
    # Extra variable names copied from the service's environment into every agent's,
    # on top of the built-in allow-list. Names starting with HERMARCHUS_ are refused.
    child_env_passthrough: str = ""

    @property
    def install_kind_names(self) -> list[str]:
        return _csv(self.install_kinds)

    @property
    def passthrough_names(self) -> list[str]:
        return _csv(self.child_env_passthrough)

    @property
    def database_path(self) -> Path:
        return self.state_dir / "hermarchus.sqlite3"

    def bearer_token(self) -> str:
        """The resolved token. Only valid after :meth:`validated`."""
        assert self.token is not None
        return self.token.get_secret_value()

    def validated(self) -> HermarchusSettings:
        """Resolve the token file and check everything a start depends on."""
        token = self.token.get_secret_value() if self.token else ""
        if not token and self.token_file is not None:
            try:
                token = self.token_file.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise SettingsError(
                    f"HERMARCHUS_TOKEN_FILE {str(self.token_file)!r} cannot be read: {exc}"
                ) from exc
        if not token:
            raise SettingsError(
                "no bearer token: set HERMARCHUS_TOKEN (or HERMARCHUS_TOKEN_FILE). Hermarchus "
                "never serves without one."
            )
        if len(token) < MIN_TOKEN_LENGTH:
            raise SettingsError(
                f"the bearer token is shorter than {MIN_TOKEN_LENGTH} characters; generate "
                "one with `openssl rand -hex 32`"
            )
        try:
            validate_tenant_id(self.profile)
        except TenantError as exc:
            raise SettingsError(f"HERMARCHUS_PROFILE: {exc}") from exc
        workspace = self.workspace_dir.resolve()
        for name, directory in (("STATE_DIR", self.state_dir), ("AGENT_HOME", self.agent_home)):
            other = directory.resolve()
            if other == workspace or workspace in other.parents or other in workspace.parents:
                raise SettingsError(
                    f"HERMARCHUS_{name} ({str(directory)!r}) overlaps HERMARCHUS_WORKSPACE_DIR "
                    f"({str(self.workspace_dir)!r}); agents reach the workspace through the "
                    "client file methods, so the spool and the home volume must sit beside it"
                )
        bad = [n for n in self.passthrough_names if n.upper().startswith("HERMARCHUS_")]
        if bad:
            raise SettingsError(
                f"HERMARCHUS_CHILD_ENV_PASSTHROUGH may not name Hermarchus' own settings: {bad}"
            )
        return self.model_copy(update={"token": SecretStr(token)})

    @classmethod
    def load(cls) -> HermarchusSettings:
        try:
            return cls().validated()
        except ValidationError as exc:
            raise SettingsError(str(exc)) from exc
