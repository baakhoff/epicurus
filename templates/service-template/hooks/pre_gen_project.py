"""Pre-generation validation: reject a name that would render an invalid or
colliding module *before* any files are written.

Cookiecutter substitutes the rendered slug/package below and runs this hook
first; a non-zero exit aborts generation with the printed reason. This guards
both ``task new-module`` and a direct ``cookiecutter`` invocation. Repo-wide
collision checks (a slug already under ``services/``, a duplicate host port) live
in ``scripts/new_module.py``, which can see the whole tree; this hook only needs
the rendered names.
"""

from __future__ import annotations

import re
import sys

SLUG = "{{ cookiecutter.service_slug }}"
PACKAGE = "{{ cookiecutter.package_name }}"

# Names already owned by the core, the web shell, or the data/edge plane — a
# module that reuses one would collide as a compose service or a module slug.
RESERVED = {
    "core-app",
    "web",
    "edge",
    "nats",
    "postgres",
    "valkey",
    "qdrant",
    "openbao",
    "openbao-unseal",
    "minio",
    "minio-init",
    "ollama",
    "searxng",
    "prometheus",
    "loki",
    "tempo",
    "grafana",
}


def _die(reason: str) -> None:
    sys.stderr.write(f"\nservice-template: {reason}\n")
    raise SystemExit(1)


if not re.fullmatch(r"[a-z][a-z0-9]*(-[a-z0-9]+)*", SLUG):
    _die(
        f"service name renders the slug {SLUG!r}, which is not a valid module slug. "
        "Use lowercase letters, digits, and single hyphens (e.g. 'My Module' -> 'my-module')."
    )

if not re.fullmatch(r"[a-z][a-z0-9_]*", PACKAGE):
    _die(f"package name {PACKAGE!r} is not a valid Python package identifier.")

if SLUG in RESERVED:
    _die(f"slug {SLUG!r} is reserved by a core/infra service - choose another name.")
