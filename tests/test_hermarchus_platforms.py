"""Hermarchus ships opt-in on both platforms at once (#1016, the all-platforms rule).

Static guarantees that the image / overlay / chart keep — what `compose-validate`,
`chart-validate` and the two smoke gates prove at runtime is checked there; these fail in
milliseconds when someone edits the files in a way that breaks a promise:

* the default `docker compose up` is untouched (the overlay is not in the root `include:`);
* the overlay binds loopback by default, uses named volumes, and refuses to resolve without
  a token;
* the image runs as a non-root user and bakes in no vendor agent program;
* the chart block is off by default, mounts no service-account token, runs non-root, and is
  covered by the network policy.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
SERVICE = REPO / "services" / "hermarchus"
CHART = REPO / "infra" / "k8s" / "epicurus"


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_the_overlay_is_not_part_of_the_default_stack() -> None:
    root = (REPO / "compose.yaml").read_text(encoding="utf-8")
    assert "hermarchus" not in root, "Hermarchus must stay an opt-in overlay (see #962's lesson)"
    assert "hermarchus" not in (REPO / "infra" / "ci" / "compose.ci.yaml").read_text("utf-8")


def test_the_overlay_binds_loopback_uses_named_volumes_and_needs_a_token() -> None:
    overlay = _load(SERVICE / "compose.yaml")
    svc = overlay["services"]["hermarchus"]
    assert svc["ports"] == ["${BIND_ADDRESS:-127.0.0.1}:${HERMARCHUS_PORT:-8094}:8080"]
    assert svc["environment"]["HERMARCHUS_TOKEN"].startswith("${HERMARCHUS_TOKEN:?")
    mounts = {v.split(":", 1)[1]: v.split(":", 1)[0] for v in svc["volumes"]}
    assert mounts == {
        "/workspace": "hermarchus-workspace",
        "/home/hermarchus": "hermarchus-home",
        "/state": "hermarchus-state",
    }
    assert set(overlay["volumes"]) == set(mounts.values())
    for source in mounts.values():
        assert not source.startswith(("/", "~", ".", "$")), "never a host path"
    assert svc["build"] == {"context": ".", "dockerfile": "services/hermarchus/Dockerfile"}
    assert svc["networks"] == ["epicurus"]


def test_the_image_is_non_root_and_carries_no_vendor_agent() -> None:
    dockerfile = (SERVICE / "Dockerfile").read_text(encoding="utf-8")
    users = re.findall(r"^USER (\S+)$", dockerfile, re.MULTILINE)
    assert users == ["hermarchus"]
    assert "useradd --uid 10001" in dockerfile
    run_lines = " ".join(
        line for line in dockerfile.splitlines() if not line.lstrip().startswith("#")
    )
    for vendor in ("claude-agent-acp", "codex-acp", "@openai/codex", "x.ai/cli", "npm install"):
        assert vendor not in run_lines, f"the image bakes in {vendor!r}"
    assert "git" in run_lines and "node" in run_lines


def test_the_chart_block_is_off_by_default_and_hardened() -> None:
    values = _load(CHART / "values.yaml")
    h = values["hermarchus"]
    assert h["enabled"] is False
    assert h["podSecurityContext"]["runAsNonRoot"] is True
    assert h["podSecurityContext"]["runAsUser"] == 10001
    assert h["podSecurityContext"]["fsGroup"] == 10001
    assert h["securityContext"]["allowPrivilegeEscalation"] is False
    assert h["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert h["resources"]["requests"] and h["resources"]["limits"]
    template = (CHART / "templates" / "hermarchus.yaml").read_text(encoding="utf-8")
    assert template.startswith("{{- if .Values.hermarchus.enabled }}")
    assert "automountServiceAccountToken: false" in template
    assert "type: Recreate" in template
    assert 'lookup "v1" "Secret"' in template, "an upgrade must not rotate the token"
    netpol = (CHART / "templates" / "networkpolicy.yaml").read_text(encoding="utf-8")
    assert "name: epicurus-hermarchus" in netpol


def test_the_chart_version_was_bumped_for_the_block() -> None:
    assert _load(CHART / "Chart.yaml")["version"] == "0.5.0"


def test_ci_validates_the_overlay_and_the_chart_block() -> None:
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "-f services/hermarchus/compose.yaml" in ci
    assert "hermarchus.enabled=true" in ci
