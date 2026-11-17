"""The two confinement rules Hermarchus enforces on agents.

* **Paths stay inside the workspace.** A session's working directory and every path an agent
  reads or writes through the client file methods must resolve — symlinks followed — to a
  location inside the workspace. ``..`` components are refused outright, before anything is
  resolved, so a request never even probes the filesystem outside.
* **Agent processes do not inherit the service's environment.** Their environment is built
  from a short allow-list, the operator's explicit pass-through names and the kind's own
  configured variables; the bearer token and every ``HERMARCHUS_*`` setting are stripped
  last, whatever asked for them.

What this does *not* do: it is not an OS sandbox. An agent program runs as the same uid as
Hermarchus and can open any path that uid can; the container (or machine) boundary is the
sandbox. These rules bound what Hermarchus itself does on an agent's behalf.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath

__all__ = [
    "BASE_ENV_ALLOWLIST",
    "PathError",
    "build_child_env",
    "confine_path",
    "resolve_workdir",
]

# Copied from the service's own environment when present. Nothing else is.
BASE_ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM")
_DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class PathError(ValueError):
    """A path that does not stay inside the workspace (or is malformed)."""


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def resolve_workdir(workspace: Path, requested: str, *, create: bool = True) -> tuple[Path, str]:
    """Resolve a session working directory; return ``(absolute path, workspace-relative)``.

    ``requested`` is relative to the workspace, or absolute and inside it. Created (with
    parents) when missing and ``create`` is set — only after the location has been proven
    to resolve inside the workspace, so a symlinked parent cannot steer the ``mkdir``.
    """
    if "\x00" in requested:
        raise PathError("the working directory contains a NUL byte")
    root = workspace.resolve()
    raw = PurePosixPath(requested.strip() or ".")
    if ".." in raw.parts:
        raise PathError("the working directory may not contain '..'")
    if raw.is_absolute():
        try:
            raw = raw.relative_to(PurePosixPath(str(root)))
        except ValueError:
            try:
                raw = raw.relative_to(PurePosixPath(str(workspace)))
            except ValueError:
                raise PathError(
                    f"the working directory {requested!r} is outside the workspace"
                ) from None
    candidate = root.joinpath(*raw.parts)
    resolved = Path(os.path.realpath(candidate))
    if not _inside(resolved, root):
        raise PathError(f"the working directory {requested!r} resolves outside the workspace")
    if resolved.exists() and not resolved.is_dir():
        raise PathError(f"the working directory {requested!r} is not a directory")
    if create:
        resolved.mkdir(parents=True, exist_ok=True)
        # Re-check after creating: a symlink swapped in between would show here.
        resolved = Path(os.path.realpath(candidate))
        if not _inside(resolved, root):
            raise PathError(f"the working directory {requested!r} resolves outside the workspace")
    relative = resolved.relative_to(root).as_posix() if resolved != root else "."
    return resolved, relative


def confine_path(workspace: Path, path: str) -> Path:
    """Resolve an agent-supplied absolute path; refuse anything outside the workspace."""
    if "\x00" in path:
        raise PathError("the path contains a NUL byte")
    raw = PurePosixPath(path)
    if not raw.is_absolute():
        raise PathError(f"the path {path!r} is not absolute (the protocol requires one)")
    if ".." in raw.parts:
        raise PathError("the path may not contain '..'")
    root = workspace.resolve()
    resolved = Path(os.path.realpath(path))
    if not _inside(resolved, root):
        raise PathError(f"the path {path!r} is outside the workspace")
    return resolved


def build_child_env(
    *,
    agent_home: Path,
    parent_env: Mapping[str, str],
    passthrough: Iterable[str] = (),
    kind_env: Mapping[str, str] | None = None,
    secrets: Iterable[str] = (),
) -> dict[str, str]:
    """The complete environment an agent process starts with.

    ``secrets`` are values that must never reach a child (the bearer token): any variable
    whose value equals one is dropped, whichever layer put it there.
    """
    env: dict[str, str] = {}
    for name in (*BASE_ENV_ALLOWLIST, *passthrough):
        if name in parent_env:
            env[name] = parent_env[name]
    home = str(agent_home)
    env["HOME"] = home
    # Programs installed into the home volume (npm -g with this prefix, pipx, curl|sh
    # installers that use ~/.local/bin) are found without the operator editing PATH.
    npm_prefix = f"{home}/.npm-global"
    env["NPM_CONFIG_PREFIX"] = npm_prefix
    base_path = env.get("PATH") or _DEFAULT_PATH
    env["PATH"] = os.pathsep.join([f"{home}/.local/bin", f"{npm_prefix}/bin", base_path])
    env.update(kind_env or {})
    blocked = {s for s in secrets if s}
    return {
        name: value
        for name, value in env.items()
        if not name.upper().startswith("HERMARCHUS_") and value not in blocked
    }
