"""The two smoke gates must keep asserting the same things (#894).

``infra/ci/smoke.sh`` (Compose) and ``infra/ci/k8s-smoke.sh`` (Kubernetes) share
``infra/ci/smoke-assert.sh`` precisely so the integration last mile is written
once. The failure mode this guards is quiet: someone adds an assertion to one gate
inline instead of to the shared file, the other runtime stops being gated on it,
and nothing goes red until a real deploy. The gates themselves cost fifteen
minutes each to run, so the invariant is checked here in milliseconds.

It also holds the CI values file to booting the *whole* stack: a module quietly
disabled in ``infra/ci/values-ci.yaml`` would drop out of ``EXPECT_MODULES``'s
reach and the Kubernetes gate would pass without ever starting it.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
CI = REPO / "infra" / "ci"
SHARED = CI / "smoke-assert.sh"
GATES = (CI / "smoke.sh", CI / "k8s-smoke.sh")

# The assertions the shared file owns, keyed by a fragment that only appears where
# that assertion is made. If one of these ever shows up in a gate script, the split
# has been undone by inlining.
_SHARED_ONLY = (
    '/platform/v1/modules"',  # module discovery through module_urls
    "/attachments",  # the chat-attachment picker
    "/platform/v1/events?module=echo",  # the module event spine
    "/platform/v1/automations/vocabulary",  # the automations surface
    "/platform/v1/llm/providers/claude/key",  # secret persistence
)


def test_both_gates_source_and_run_the_shared_assertions() -> None:
    for gate in GATES:
        text = gate.read_text(encoding="utf-8")
        assert '. "$ROOT/infra/ci/smoke-assert.sh"' in text, (
            f"{gate.name} does not source the shared assertions"
        )
        assert re.search(r"^\s*smoke_assert\s*$", text, re.MULTILINE), (
            f"{gate.name} sources the shared assertions but never runs them"
        )
        assert "smoke_modules" in text, (
            f"{gate.name} does not derive its module set from smoke_modules"
        )


def test_no_gate_reimplements_a_shared_assertion() -> None:
    shared = SHARED.read_text(encoding="utf-8")
    for needle in _SHARED_ONLY:
        assert needle in shared, f"{needle!r} is no longer in smoke-assert.sh — update this test"
    for gate in GATES:
        text = gate.read_text(encoding="utf-8")
        inlined = [needle for needle in _SHARED_ONLY if needle in text]
        assert not inlined, (
            f"{gate.name} asserts {inlined} itself — it belongs in infra/ci/smoke-assert.sh "
            "so the other runtime is gated on it too"
        )


def test_the_shared_file_declares_the_callbacks_it_needs() -> None:
    """Both gates must define every hook the shared assertions call."""
    shared = SHARED.read_text(encoding="utf-8")
    hooks = [name for name in ("restart_openbao", "restart_core_app") if name in shared]
    assert hooks, "smoke-assert.sh calls no restart hook — update this test"
    for gate in GATES:
        text = gate.read_text(encoding="utf-8")
        for hook in hooks:
            assert re.search(rf"^{hook}\(\)", text, re.MULTILINE), (
                f"{gate.name} does not define {hook}(), which smoke-assert.sh calls"
            )


def test_ci_values_boot_the_whole_stack() -> None:
    """The Kubernetes gate must not silently skip a module or a data-plane piece.

    Ollama is the one deliberate exception (a multi-gigabyte image and a 4Gi
    request for a model the gate never uses); everything else the chart enables by
    default has to still be enabled, or the boot proves less than it claims.
    """
    values = yaml.safe_load((CI / "values-ci.yaml").read_text(encoding="utf-8"))
    assert values.get("ollama", {}).get("enabled") is False, (
        "values-ci.yaml no longer disables Ollama — if that is deliberate, update this test"
    )
    for section in ("postgres", "nats", "qdrant", "openbao", "minio"):
        assert values.get(section, {}).get("enabled") is not False, (
            f"values-ci.yaml disables {section}: the k8s-smoke gate would stop proving it boots"
        )
    assert values.get("web", {}).get("enabled") is not False, (
        "values-ci.yaml disables web: the nginx-resolver assertion (#891) needs it"
    )
    for name, cfg in (values.get("modules") or {}).items():
        assert cfg.get("enabled") is not False, (
            f"values-ci.yaml disables the {name} module: the gate would never boot it"
        )
