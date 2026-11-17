"""The agent-kind registry: kind name -> how to start a program that speaks ACP.

Three layers, later wins by name:

1. the built-in ``echo`` reference agent (this package, run with the service's own Python);
2. the shipped entries in ``kinds.yaml`` (Claude Code, Codex, Grok Build — documented,
   **not verified by running**);
3. an operator file named by ``HERMARCHUS_KINDS_FILE``, same shape; ``enabled: false``
   removes a kind.

A kind is *available* when its command resolves on the PATH an agent process would get
(the home volume's bin directories first); otherwise ``/v1/info`` says why.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

__all__ = ["ECHO_KIND", "AgentKind", "KindError", "load_kinds"]

ECHO_KIND = "echo"
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


class KindError(ValueError):
    """A kinds file that cannot be used; the message names the entry and the fix."""


@dataclass(frozen=True)
class AgentKind:
    kind: str
    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    install: str | None = None
    docs: tuple[str, ...] = ()
    builtin: bool = False
    # Shipped entries are documented, never run here; the operator's own are theirs.
    verified: bool = False

    def argv(self) -> list[str]:
        return [self.command, *self.args]

    def resolve(self, path: str) -> str | None:
        """The executable this kind would run, on the given PATH, or ``None``."""
        if os.sep in self.command:
            p = Path(self.command)
            return str(p) if p.is_file() and os.access(p, os.X_OK) else None
        return shutil.which(self.command, path=path)

    def unavailable_reason(self, path: str) -> str | None:
        if self.resolve(path) is not None:
            return None
        hint = (
            f" — install it into the home volume (`{self.install}`, or list the kind in "
            "HERMARCHUS_INSTALL_KINDS), then sign in to it inside the instance"
            if self.install
            else ""
        )
        return f"command {self.command!r} not found on the agent PATH{hint}"


def _echo_kind() -> AgentKind:
    return AgentKind(
        kind=ECHO_KIND,
        name="Echo (reference agent)",
        command=sys.executable,
        args=("-m", "epicurus_hermarchus.echo_agent"),
        builtin=True,
        verified=True,
    )


def _str_list(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, list) or not all(isinstance(v, str | int | float) for v in value):
        raise KindError(f"{where} must be a list of strings")
    return tuple(str(v) for v in value)


def _parse_entry(kind: str, raw: Any, *, shipped: bool) -> AgentKind | None:
    where = f"kind {kind!r}"
    if not _NAME_RE.fullmatch(kind):
        raise KindError(f"{where}: a kind name is lowercase letters, digits, '.', '_' or '-'")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise KindError(f"{where} must be a mapping")
    if raw.get("enabled", True) is False:
        return None
    command = raw.get("command")
    if not isinstance(command, str) or not command.strip():
        raise KindError(f"{where} needs a `command`")
    env_raw = raw.get("env") or {}
    if not isinstance(env_raw, dict):
        raise KindError(f"{where}: `env` must be a mapping of NAME: value")
    env = {str(k): str(v) for k, v in env_raw.items()}
    bad = [k for k in env if k.upper().startswith("HERMARCHUS_")]
    if bad:
        raise KindError(f"{where}: `env` may not set Hermarchus' own settings ({bad})")
    install = raw.get("install")
    if install is not None and not isinstance(install, str):
        raise KindError(f"{where}: `install` must be a shell command string")
    return AgentKind(
        kind=kind,
        name=str(raw.get("name") or kind),
        command=command.strip(),
        args=_str_list(raw.get("args"), f"{where}.args"),
        env=env,
        install=install or None,
        docs=_str_list(raw.get("docs"), f"{where}.docs"),
        verified=bool(raw.get("verified", not shipped)),
    )


def _parse_document(text: str, source: str, *, shipped: bool) -> dict[str, AgentKind | None]:
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise KindError(f"{source} is not valid YAML: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("kinds", {}), dict):
        raise KindError(f"{source} must be a mapping with a `kinds:` mapping")
    out: dict[str, AgentKind | None] = {}
    for kind, raw in (doc.get("kinds") or {}).items():
        out[str(kind)] = _parse_entry(str(kind), raw, shipped=shipped)
    return out


def load_kinds(kinds_file: Path | None = None) -> dict[str, AgentKind]:
    """The effective registry, ordered by kind name with ``echo`` first."""
    registry: dict[str, AgentKind | None] = {ECHO_KIND: _echo_kind()}
    shipped = resources.files("epicurus_hermarchus").joinpath("kinds.yaml").read_text("utf-8")
    registry.update(_parse_document(shipped, "the shipped kinds.yaml", shipped=True))
    if kinds_file is not None:
        try:
            text = kinds_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise KindError(f"HERMARCHUS_KINDS_FILE {str(kinds_file)!r}: {exc}") from exc
        operator = _parse_document(text, str(kinds_file), shipped=False)
        if operator.get(ECHO_KIND, registry[ECHO_KIND]) is not registry[ECHO_KIND]:
            raise KindError("the built-in `echo` kind cannot be redefined or disabled")
        registry.update(operator)
    ordered = sorted(k for k in registry if k != ECHO_KIND)
    result = {ECHO_KIND: registry[ECHO_KIND]}
    result.update({k: registry[k] for k in ordered})
    return {k: v for k, v in result.items() if v is not None}
