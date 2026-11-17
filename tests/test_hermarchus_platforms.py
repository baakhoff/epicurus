"""Hermarchus ships opt-in on both platforms at once (#1016, the all-platforms rule).

Static guarantees that the image / overlay / chart keep — what `compose-validate`,
`chart-validate` and the two smoke gates prove at runtime is checked there; these fail in
milliseconds when someone edits the files in a way that breaks a promise:

* the default `docker compose up` is untouched (the overlay is not in the root `include:`);
* the overlay binds loopback by default, uses named volumes, and refuses to resolve without
  a token;
* the image runs as a non-root user and bakes in no vendor agent program;
* the chart block is off by default, mounts no service-account token, runs non-root, and is
  covered by the network policy;
* the agent host cannot reach the stack: on Compose it is on a network of its own, on
  Kubernetes its NetworkPolicy restricts egress to DNS and public addresses and no in-release
  policy admits it.
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
    assert svc["networks"] == ["hermarchus"]


def test_the_overlay_keeps_the_agent_host_off_the_stack_network() -> None:
    """Agents run arbitrary code; on `epicurus` they could call every module's MCP endpoint,
    the core's platform API and model gateway, and the data plane directly."""
    overlay = _load(SERVICE / "compose.yaml")
    assert "epicurus" not in overlay["services"]["hermarchus"]["networks"]
    assert set(overlay["networks"]) == {"hermarchus"}
    assert not overlay["networks"]["hermarchus"], "a plain bridge: internet egress, no stack"
    ci = (REPO / "infra" / "ci" / "compose.hermarchus.yaml").read_text(encoding="utf-8")
    assert "name: ${COMPOSE_PROJECT_NAME:-epicurus-ci}-hermarchus" in ci
    smoke = (REPO / "infra" / "ci" / "smoke.sh").read_text(encoding="utf-8")
    assert 'HNET="${COMPOSE_PROJECT_NAME}-hermarchus"' in smoke
    for target in ("core-app:8080", "echo:8080", "postgres:5432", "qdrant:6333"):
        assert target in smoke, f"the Compose gate no longer proves {target} is unreachable"


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


def _policy(netpol: str, name: str) -> str:
    start = netpol.index(f"name: {name}\n")
    end = netpol.find("\n---", start)
    return netpol[start : end if end != -1 else len(netpol)]


def test_the_chart_policy_restricts_the_agent_host_egress_and_admits_it_nowhere() -> None:
    netpol = (CHART / "templates" / "networkpolicy.yaml").read_text(encoding="utf-8")
    own = _policy(netpol, "epicurus-hermarchus")
    assert 'policyTypes: ["Ingress", "Egress"]' in own
    assert "k8s-app: kube-dns" in own and "port: 53" in own
    assert "cidr: 0.0.0.0/0" in own
    for private in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "100.64.0.0/10",
        "fc00::/7",
        "fe80::/10",
    ):
        assert f"- {private}" in own, f"egress no longer excludes {private}"
    assert ".Values.hermarchus.egressExtra" in own
    assert _load(CHART / "values.yaml")["hermarchus"]["egressExtra"] == []
    # The pod carries part-of=epicurus (the chart's common labels), so every in-release
    # ingress rule that admits by that label must exclude the agent host.
    for name in ("epicurus-core-app", "epicurus-data-plane", "epicurus-hermarchus"):
        rule = _policy(netpol, name)
        assert "app.kubernetes.io/part-of: epicurus" in rule
        assert 'operator: NotIn\n                values: ["hermarchus"]' in rule, name


def test_the_chart_version_was_bumped_for_the_block() -> None:
    assert _load(CHART / "Chart.yaml")["version"] == "0.5.0"


def test_ci_validates_the_overlay_and_the_chart_block() -> None:
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "-f services/hermarchus/compose.yaml" in ci
    assert "hermarchus.enabled=true" in ci
