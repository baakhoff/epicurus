#!/usr/bin/env python
"""Scaffold a new epicurus module **and wire it into the stack** (issue #113).

Wave 2 cost a day because parallel agents diverged on ports and each hand-rolled
the wire-in steps. This wraps the ``templates/service-template`` cookiecutter and
performs every step the runtime smoke gate enforces, so a freshly scaffolded
module passes ``task smoke`` with no manual edits:

  1. pick the next free host port from the registry (or validate ``--port``);
  2. render the template into ``services/<slug>/``;
  3. register the package in the root ``pyproject.toml`` (mypy + ruff isort);
  4. add the fragment to the top-level ``compose.yaml`` ``include:`` list;
  5. register ``http://<slug>:8080`` in the core's ``module_urls``;
  6. reset its host port in the smoke override (``infra/ci/compose.ci.yaml``);
  7. refresh ``uv.lock`` so the Docker build (``uv sync --frozen``) sees it.

Usage::

    uv run python scripts/new_module.py "My Module"
    uv run python scripts/new_module.py "My Module" --port 8095
    uv run python scripts/new_module.py "My Module" --no-sync   # skip uv lock

The port-scan and wiring helpers are pure text transforms (no I/O of their own
beyond the caller) so ``tests/`` can exercise them against a throwaway repo copy.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

# New modules take the next free port in this band; echo (8080) sits just below
# it. Kept in sync with docs/reference/ports.md.
PORT_BAND = range(8082, 8100)

SETTINGS_REL = "services/core-app/src/epicurus_core_app/settings.py"
CI_OVERRIDE_REL = "infra/ci/compose.ci.yaml"

# Host-port defaults are always written as ``${NAME:-1234}:<container>`` in this
# repo's compose fragments; a defensive second pattern catches a literal
# ``1.2.3.4:1234:<container>`` form should one ever be added by hand.
_ENV_PORT = re.compile(r"\$\{[A-Z0-9_]+:-(\d+)\}:\d+")
_LITERAL_PORT = re.compile(r'"\d+\.\d+\.\d+\.\d+:(\d+):\d+"')

_SLUG_RE = re.compile(r"[a-z][a-z0-9]*(-[a-z0-9]+)*")
_INCLUDE_RE = re.compile(r"\s*-\s*services/.+/compose\.yaml")


def repo_root() -> Path:
    """The repository root (this script lives in ``scripts/``)."""
    return Path(__file__).resolve().parents[1]


def slugify(service_name: str) -> str:
    """Mirror the template's slug derivation exactly (cookiecutter.json)."""
    return service_name.lower().replace(" ", "-").replace("_", "-")


def package_name(slug: str) -> str:
    """Mirror the template's package derivation exactly (cookiecutter.json)."""
    return "epicurus_" + slug.replace("-", "_")


def _compose_files(root: Path) -> list[Path]:
    """Every compose fragment whose published host ports could collide."""
    files = sorted(root.glob("services/*/compose.yaml"))
    files += sorted(p for p in root.glob("infra/**/*.yml"))
    files += sorted(p for p in root.glob("infra/**/*.yaml"))
    return files


def port_bindings(root: Path) -> list[tuple[int, str]]:
    """Every ``(host_port, fragment)`` binding across the stack, in scan order.

    The single source of truth is the compose fragments themselves (what would
    actually bind), so the registry can never drift from reality. A host port
    appearing more than once here is a collision (``tests/test_compose_ports.py``).
    """
    bindings: list[tuple[int, str]] = []
    for path in _compose_files(root):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(root).as_posix()
        for match in _ENV_PORT.finditer(text):
            bindings.append((int(match.group(1)), rel))
        for match in _LITERAL_PORT.finditer(text):
            bindings.append((int(match.group(1)), rel))
    return bindings


def published_ports(root: Path) -> dict[int, str]:
    """Map each published host port to the first fragment that binds it."""
    seen: dict[int, str] = {}
    for port, fragment in port_bindings(root):
        seen.setdefault(port, fragment)
    return seen


def next_free_port(root: Path) -> int:
    """Lowest unused host port in the module band."""
    used = set(published_ports(root))
    for port in PORT_BAND:
        if port not in used:
            return port
    raise SystemExit(
        f"port band {PORT_BAND.start}-{PORT_BAND.stop - 1} is exhausted — "
        "widen PORT_BAND in scripts/new_module.py and docs/reference/ports.md."
    )


def _add_to_toml_array(text: str, key: str, value: str) -> str:
    """Append ``"value"`` to the single-line TOML array ``key = [...]`` (idempotent)."""
    pattern = re.compile(rf"(?m)^{re.escape(key)} = \[(?P<inner>[^\]]*)\]")
    match = pattern.search(text)
    if match is None:
        raise RuntimeError(f"could not find `{key} = [...]` in pyproject.toml")
    inner = match.group("inner")
    if f'"{value}"' in inner:
        return text
    trimmed = inner.rstrip()
    joined = f'{trimmed} "{value}"' if trimmed.endswith(",") else f'{trimmed}, "{value}"'
    return text[: match.start("inner")] + joined + text[match.end("inner") :]


def wire_pyproject(root: Path, pkg: str) -> None:
    """Register the package with mypy and ruff's isort (root pyproject.toml)."""
    path = root / "pyproject.toml"
    text = path.read_text(encoding="utf-8")
    text = _add_to_toml_array(text, "known-first-party", pkg)
    text = _add_to_toml_array(text, "packages", pkg)
    path.write_text(text, encoding="utf-8")


def wire_compose_include(root: Path, slug: str) -> None:
    """Add the module fragment to the top-level compose include list (idempotent)."""
    path = root / "compose.yaml"
    text = path.read_text(encoding="utf-8")
    entry = f"  - services/{slug}/compose.yaml"
    lines = text.splitlines()
    if entry in lines:
        return
    service_idx = [i for i, line in enumerate(lines) if _INCLUDE_RE.match(line)]
    insert_at = (service_idx[-1] + 1) if service_idx else len(lines)
    lines.insert(insert_at, entry)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def wire_module_urls(root: Path, slug: str) -> None:
    """Append ``http://<slug>:8080`` to the core's ``module_urls`` (idempotent).

    Re-emits the literal one URL per line: format-stable (ruff leaves implicit
    string concatenation alone) and never overflows the 100-col line limit, which
    a single growing line eventually would.
    """
    path = root / SETTINGS_REL
    text = path.read_text(encoding="utf-8")
    new_url = f"http://{slug}:8080"
    match = re.search(r'module_urls:\s*str\s*=\s*\(\s*((?:"[^"]*"\s*)+)\)', text)
    if match is None:
        raise RuntimeError("could not locate the module_urls literal in settings.py")
    value = "".join(re.findall(r'"([^"]*)"', match.group(1)))
    urls = [u.strip() for u in value.split(",") if u.strip()]
    if new_url in urls:
        return
    urls.append(new_url)
    body = "\n".join(
        f'        "{url}{"," if i < len(urls) - 1 else ""}"' for i, url in enumerate(urls)
    )
    literal = f"module_urls: str = (\n{body}\n    )"
    path.write_text(text[: match.start()] + literal + text[match.end() :], encoding="utf-8")


def wire_ci_port_reset(root: Path, slug: str) -> None:
    """Add the module to the smoke CI override so it publishes no host port there.

    The override resets every booted service's ports (``infra/ci/compose.ci.yaml``);
    a module missing from it leaks its host binding and collides with a running dev
    stack. Idempotent.
    """
    path = root / CI_OVERRIDE_REL
    text = path.read_text(encoding="utf-8")
    if f"\n  {slug}:\n" in text:
        return
    marker = "\nservices:\n"
    idx = text.index(marker) + len(marker)
    path.write_text(
        text[:idx] + f"  {slug}:\n    ports: !reset []\n" + text[idx:], encoding="utf-8"
    )


def scaffold(root: Path, service_name: str, port: int, *, output_dir: Path) -> Path:
    """Render the cookiecutter template and return the generated service dir."""
    from cookiecutter.main import cookiecutter

    out = cookiecutter(
        str(root / "templates" / "service-template"),
        no_input=True,
        output_dir=str(output_dir),
        extra_context={"service_name": service_name, "port": str(port)},
    )
    return Path(out)


def _uv_lock(root: Path) -> None:
    subprocess.run(["uv", "lock"], cwd=root, check=True)


class ScaffoldResult(NamedTuple):
    """What ``run`` produced — also drives the CLI summary."""

    service_dir: Path
    slug: str
    package: str
    port: int


def run(
    root: Path, service_name: str, *, port: int | None = None, sync: bool = True
) -> ScaffoldResult:
    """Scaffold ``service_name`` under ``root`` and wire it into the stack.

    Raises ``SystemExit`` with a message on any precondition failure (bad name,
    slug already taken, requested port already published).
    """
    slug = slugify(service_name)
    if _SLUG_RE.fullmatch(slug) is None:
        raise SystemExit(
            f"name {service_name!r} renders the slug {slug!r}, which is not a valid module slug "
            "(lowercase letters, digits, single hyphens)."
        )
    pkg = package_name(slug)

    if (root / "services" / slug).exists():
        raise SystemExit(f"services/{slug} already exists — pick a different name.")

    used = published_ports(root)
    if port is not None:
        if port in used:
            raise SystemExit(f"port {port} is already published by {used[port]} — pick another.")
        chosen = port
    else:
        chosen = next_free_port(root)

    service_dir = scaffold(root, service_name, chosen, output_dir=root / "services")
    wire_pyproject(root, pkg)
    wire_compose_include(root, slug)
    wire_module_urls(root, slug)
    wire_ci_port_reset(root, slug)
    if sync:
        _uv_lock(root)
    return ScaffoldResult(service_dir, slug, pkg, chosen)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scaffold and wire in a new epicurus module.")
    parser.add_argument("name", help='Human service name, e.g. "Weather" or "My Module".')
    parser.add_argument("--port", type=int, default=None, help="Host port (default: next free).")
    parser.add_argument("--no-sync", action="store_true", help="Skip the uv.lock refresh.")
    args = parser.parse_args(argv)

    root = repo_root()
    result = run(root, args.name, port=args.port, sync=not args.no_sync)

    rel = result.service_dir.relative_to(root).as_posix()
    wired = "pyproject (mypy + ruff), compose include, core module_urls, CI port reset"
    print(f"\nScaffolded and wired in '{result.slug}' at {rel} (host port {result.port}).")
    print(f"Wired: {wired}{'' if args.no_sync else ', uv.lock'}")
    print("\nNext:")
    print("  uv sync --all-packages")
    print(f"  uv run pytest services/{result.slug}")
    print("  task smoke            # boot the stack and assert the integration last mile")
    print(f"\nReplace the sample `ping` tool in {rel}/src/{result.package}/service.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
