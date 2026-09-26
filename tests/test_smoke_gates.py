"""The two smoke gates must keep asserting the same things (#894, #919).

``infra/ci/smoke.sh`` (Compose) and ``infra/ci/k8s-smoke.sh`` (Kubernetes) share
``infra/ci/smoke-assert.sh`` precisely so the integration last mile is written
once. The failure mode this guards is quiet: someone adds an assertion to one gate
inline instead of to the shared file, the other runtime stops being gated on it,
and nothing goes red until a real deploy. The gates themselves cost fifteen
minutes each to run, so the invariant is checked here in milliseconds.

It also holds the CI values file to booting the *whole* stack — and holds *this
file* to meaning something. The #894 review found it guarding less than it claimed:
``"smoke_modules" in text`` was satisfied by a comment, and the ``web`` / ``modules``
checks were vacuous because ``values-ci.yaml`` has neither key, so they asserted
``None is not False`` forever. Both are now derived from a source of truth that
cannot silently go missing: the gate's actual assignment, and the chart's own
default values.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
CI = REPO / "infra" / "ci"
CHART = REPO / "infra" / "k8s" / "epicurus"
SHARED = CI / "smoke-assert.sh"
STUB = CI / "ollama-stub.yaml"
GATES = (CI / "smoke.sh", CI / "k8s-smoke.sh")

# The assertions the shared file owns, keyed by a fragment that only appears where
# that assertion is made. If one of these ever shows up in a gate script, the split
# has been undone by inlining.
_SHARED_ONLY = (
    '/platform/v1/modules"',  # module discovery through module_urls
    "http://web:8080/healthz",  # the web shell's runtime-derived nginx resolver (#891)
    "path=$FILE_PATH",  # the tenant file space round trip (#919)
    "/platform/v1/llm/prefs/kv-cache-type",  # the KV-cache apply through the seam (#307)
    "/attachments",  # the chat-attachment picker
    "/platform/v1/events?module=echo",  # the module event spine
    "/platform/v1/automations/vocabulary",  # the automations surface
    "/platform/v1/llm/providers/claude/key",  # secret persistence
)

# Every hook smoke-assert.sh calls back into. Named here *and* required to still be
# present in the shared file, so dropping one from the contract fails loudly instead
# of quietly shrinking what this test checks.
_HOOKS = ("restart_openbao", "restart_core_app", "settle_llm_runtime", "enable_sign_in")

# The label pair KubernetesController._workloads selects on and _owns re-reads before
# it patches anything (services/core-app/src/epicurus_core_app/kubernetes_control.py).
_PART_OF_LABEL = "app.kubernetes.io/part-of"
_COMPONENT_LABEL = "app.kubernetes.io/component"


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_both_gates_source_and_run_the_shared_assertions() -> None:
    for gate in GATES:
        text = gate.read_text(encoding="utf-8")
        assert '. "$ROOT/infra/ci/smoke-assert.sh"' in text, (
            f"{gate.name} does not source the shared assertions"
        )
        assert re.search(r"^\s*smoke_assert\s*$", text, re.MULTILINE), (
            f"{gate.name} sources the shared assertions but never runs them"
        )
        # The *assignment*, not the word: `"smoke_modules" in text` was satisfied by a
        # comment mentioning it, so a gate could have hardcoded its module list and
        # still passed (#919).
        assert re.search(r'^EXPECT_MODULES="\$\(smoke_modules\)"', text, re.MULTILINE), (
            f"{gate.name} does not derive EXPECT_MODULES from smoke_modules — "
            "a hand-kept module list drifts the moment a module is wired in"
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
    missing = [name for name in _HOOKS if name not in shared]
    assert not missing, (
        f"smoke-assert.sh no longer calls {missing} — if a hook was retired, retire it here too"
    )
    for gate in GATES:
        text = gate.read_text(encoding="utf-8")
        for hook in _HOOKS:
            assert re.search(rf"^{hook}\(\)", text, re.MULTILINE), (
                f"{gate.name} does not define {hook}(), which smoke-assert.sh calls"
            )


def test_both_gates_assert_the_bucket_seed() -> None:
    """`minio-init` was asserted by neither gate, so a failed seed was invisible (#919).

    It cannot live in the shared file — one runtime asks a container's exit code and the
    other a Job's condition — so the parity is checked from here instead.
    """
    for gate in GATES:
        assert "minio-init" in gate.read_text(encoding="utf-8"), (
            f"{gate.name} never asserts the minio-init one-shot: a failed bucket seed would "
            "stay invisible until an operator's first upload"
        )


def test_the_kubernetes_gate_upgrades_as_well_as_installs() -> None:
    """Every k8s boot was a fresh install until #919; operators upgrade."""
    text = (CI / "k8s-smoke.sh").read_text(encoding="utf-8")
    assert re.search(r"^helm upgrade ", text, re.MULTILINE), (
        "k8s-smoke.sh never runs `helm upgrade` — the restart/upgrade defect class "
        "(the OpenBao DAC_OVERRIDE bug) is the one a fresh install cannot show"
    )


def test_ci_values_boot_the_whole_stack() -> None:
    """The Kubernetes gate must not silently skip a module or a data-plane piece.

    Enumerated from the *chart's own* defaults rather than from the CI values, so a
    component the CI file simply never mentions is still checked — the shape that made
    the previous version of this test vacuous.

    Ollama is the one deliberate exception (a multi-gigabyte image and a 4Gi request
    for a model the gate never uses), and it is compensated: the gate applies
    ``infra/ci/ollama-stub.yaml`` so the restart arm still runs against a real
    StatefulSet. That compensation is pinned by its own test below.
    """
    defaults = _load(CHART / "values.yaml")
    values = _load(CI / "values-ci.yaml")

    assert values.get("ollama", {}).get("enabled") is False, (
        "values-ci.yaml no longer disables Ollama — if that is deliberate, the stub in "
        "infra/ci/ollama-stub.yaml collides with the chart's own `ollama` StatefulSet"
    )

    checked = 0
    for section, cfg in defaults.items():
        if section in ("ollama", "modules") or not isinstance(cfg, dict):
            continue
        if cfg.get("enabled") is not True:
            continue  # off by default (ingress, networkPolicy…): not what this guards
        checked += 1
        assert (values.get(section) or {}).get("enabled") is not False, (
            f"values-ci.yaml disables {section}, which the chart enables by default: "
            "the k8s-smoke gate would stop proving it boots"
        )
    assert checked >= 5, "the chart suddenly enables almost nothing by default — read this"

    modules = defaults.get("modules") or {}
    assert modules, "the chart declares no modules — update this test"
    for name in modules:
        assert ((values.get("modules") or {}).get(name) or {}).get("enabled") is not False, (
            f"values-ci.yaml disables the {name} module: the gate would never boot it"
        )


def test_the_ollama_stub_stands_in_for_the_workload_the_restart_arm_looks_for() -> None:
    """The stub only closes the hole if the seam can actually find and patch it (#919).

    ``KubernetesController.restart_service`` selects on ``part-of=epicurus`` +
    ``component=ollama``, re-reads the component label off the object, and tries
    **StatefulSets before Deployments** — so a Deployment stub would leave the chart
    Role's ``statefulsets`` verb untested, which is the hole. Rename any of this and the
    gate goes green while asserting nothing.
    """
    stub = _load(STUB)
    assert stub["kind"] == "StatefulSet", (
        "the Ollama stand-in must be a StatefulSet — a Deployment would be matched by the "
        "seam's second pass and leave the chart Role's `statefulsets` verb unexercised"
    )
    assert stub["metadata"]["name"] == "ollama"
    labels = stub["metadata"]["labels"]
    assert labels[_PART_OF_LABEL] == "epicurus"
    assert labels[_COMPONENT_LABEL] == "ollama"
    pod_labels = stub["spec"]["template"]["metadata"]["labels"]
    assert pod_labels[_COMPONENT_LABEL] == "ollama"

    gate = (CI / "k8s-smoke.sh").read_text(encoding="utf-8")
    assert "infra/ci/ollama-stub.yaml" in gate, (
        "k8s-smoke.sh does not apply the Ollama stand-in — with Ollama disabled in "
        "values-ci.yaml the seam's restart arm has no workload to find"
    )


def test_the_compose_gate_starts_the_web_shell() -> None:
    """The shared file asserts web's proxy; the Compose gate has to actually boot it."""
    text = (CI / "smoke.sh").read_text(encoding="utf-8")
    assert re.search(r'^APP="core-app web \$EXPECT_MODULES"', text, re.MULTILINE), (
        "smoke.sh no longer starts `web`: the shared nginx-resolver assertion (#891) "
        "would fail, or worse, be moved back out of the shared file"
    )


def test_both_gates_run_the_sign_in_phase_last() -> None:
    """#969's "proof on both": each gate turns sign-in on after everything else has passed.

    Last, because the phase closes the web door — an assertion after it that went through
    ``web`` would fail for a reason that has nothing to do with what it asserts.
    """
    shared = SHARED.read_text(encoding="utf-8")
    assert re.search(r"^smoke_assert_sign_in\(\) \{", shared, re.MULTILINE)
    for needle in ("/platform/v1/auth/session", "auth_error=provider_unreachable", "storage_list"):
        assert needle in shared, f"the sign-in phase no longer asserts {needle!r}"
    for gate in GATES:
        lines = [line.strip() for line in gate.read_text(encoding="utf-8").splitlines()]
        calls = [i for i, line in enumerate(lines) if line == "smoke_assert_sign_in"]
        assert calls, f"{gate.name} never runs the sign-in phase"
        assert calls[0] > lines.index("smoke_assert"), (
            f"{gate.name} runs the sign-in phase before the shared last mile"
        )


def test_the_sign_in_phase_turns_oidc_on_without_colliding_with_the_chart_or_compose() -> None:
    """The mechanisms survive the core-app fragment and the chart both carrying AUTH_MODE.

    Compose merges an override onto core-app's ``environment`` by key; Kubernetes uses
    ``kubectl set env``, which updates by name — never ``core.extraEnv``, which would render a
    second ``AUTH_MODE`` entry beside the one the chart's ``auth:`` block emits.
    """
    override = _load(CI / "compose.auth.yaml")
    env = override["services"]["core-app"]["environment"]
    assert env["AUTH_MODE"] == "oidc"
    assert env["OIDC_ISSUER_URL"] and env["OIDC_CLIENT_ID"]
    assert str(env["OIDC_ALLOW_ALL_USERS"]).lower() == "true"
    assert set(override["services"]) == {"core-app"}, "the override must touch core-app only"
    assert "-f infra/ci/compose.auth.yaml up -d --no-deps core-app" in (CI / "smoke.sh").read_text(
        encoding="utf-8"
    )

    k8s = (CI / "k8s-smoke.sh").read_text(encoding="utf-8")
    assert "kc set env deployment/core-app" in k8s and "AUTH_MODE=oidc" in k8s
    assert "extraEnv.AUTH_MODE" not in k8s
