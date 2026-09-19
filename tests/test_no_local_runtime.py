"""A deployment may run no local LLM runtime at all — on either runtime (#962, ADR-0144).

Three states, not two: the stack's own Ollama, an external one, and *none*. The third was
unreachable on both runtimes — the Helm chart `required`d an external URL the moment Ollama
was disabled, and Compose included the fragment unconditionally with `core-app` hard-depending
on the service. These are the static and render-time halves of the fix:

* **Compose** — parsed from the fragments directly, in the style of ``test_compose_ports.py``:
  the opt-out overlay that removes Ollama, the `required: false` that stops its absence
  failing the whole `up`, and the env interpolation that carries a *deliberately blank*
  ``OLLAMA_URL`` through to the core. And, first, the thing this feature could most easily
  break for people who never asked for it: the **documented install** — `git clone` &&
  `docker compose up -d`, no `.env`, no flags — must still bring up the local runtime.
* **Chart** — real ``helm template`` renders (no cluster), because a guard nobody renders is a
  guard that has quietly stopped guarding. Skipped, not silently passed, without ``helm``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
CHART = REPO / "infra" / "k8s" / "epicurus"
OLLAMA_FRAGMENT = REPO / "infra" / "ollama" / "compose.yaml"
CORE_FRAGMENT = REPO / "services" / "core-app" / "compose.yaml"

HOSTED_ONLY_OVERLAY = REPO / "infra" / "ollama" / "compose.hosted-only.yaml"

# The services that *are* the local runtime. Named here so a rename has to come past these
# assertions rather than quietly making them vacuous.
_LOCAL_AI_SERVICES = ("ollama", "ollama-init")

# Every Taskfile command that starts the stack for ordinary use. None of them may need a flag
# to get local AI: the flagless path is the one the docs publish.
_START_TASKS = ("up", "obs-up", "docker-socket-up", "external-mounts-up")


def _fragment(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


# ── Compose ──────────────────────────────────────────────────────────────────────


def test_the_default_install_still_brings_up_the_local_runtime() -> None:
    """The regression guard on the *documented* install path.

    `README.md` and `docs/user/installation.md` both publish `git clone` → `cd epicurus` →
    `docker compose up -d`, and neither mentions an `.env` before that step. So whatever
    expresses "no local runtime" must be something the operator **adds**, never something the
    default path has to remember to select. The first cut of #962 used a compose profile on
    these services; a profile is opt-in, so it removed Ollama from every fresh clone while the
    shipped `llama3.2` / `nomic-embed-text` defaults still pointed at it — the half-working
    stack the chart's render-time guard refuses, delivered by default.

    Asserted on the fragments (no Docker): nothing may gate these services behind a profile,
    and no start command may need a flag to get them. `compose-validate` proves the same thing
    against a real `docker compose config`.
    """
    services = _fragment(OLLAMA_FRAGMENT)["services"]
    for name in _LOCAL_AI_SERVICES:
        assert "profiles" not in services[name], (
            f"{name} is behind a compose profile, so `docker compose up -d` from a fresh "
            "clone no longer starts the local runtime — use the opt-out overlay "
            "(infra/ollama/compose.hosted-only.yaml) instead (#962)"
        )

    taskfile = yaml.safe_load((REPO / "Taskfile.yml").read_text(encoding="utf-8"))
    for name in _START_TASKS:
        cmds = " ".join(str(c) for c in taskfile["tasks"][name]["cmds"])
        assert "hosted-only" not in cmds and "--profile local-ai" not in cmds, (
            f"task {name} should start the local runtime with no extra selection"
        )

    reconcile = (REPO / "infra" / "cd" / "reconcile.sh").read_text(encoding="utf-8")
    assert "EPICURUS_HOSTED_ONLY" in reconcile, (
        "the deploy path has no way to opt out of the local runtime"
    )
    assert "EPICURUS_HOSTED_ONLY:-0" in reconcile, (
        "the deploy path must default to *keeping* the local runtime — a box that never "
        "asked for a hosted-only stack must not lose Ollama on its next reconcile"
    )


def test_the_local_runtime_can_be_left_out_through_the_overlay() -> None:
    """The opt-out, and both halves of it.

    Removing the container alone leaves a core still pointed at `http://ollama:11434` — that
    is *unreachable*, not *absent*, and the whole point of #962 is that those are different
    facts. The overlay does both, so `task hosted-only-up` is one command and cannot be
    half-applied.
    """
    overlay = _fragment(HOSTED_ONLY_OVERLAY)["services"]

    for name in _LOCAL_AI_SERVICES:
        profiles = overlay[name].get("profiles")
        assert profiles, f"the overlay does not remove {name}"
        # An override file can add to the compose model but not delete from it, so assigning a
        # profile nothing enables is how a service is taken out. Any profile name works; what
        # must stay true is that it is not one this repo ever activates.
        for profile in profiles:
            assert profile != "observability", (
                "the overlay parks a service on a profile the repo actually enables, so the "
                "removal would undo itself"
            )

    assert overlay["core-app"]["environment"]["OLLAMA_URL"] == "", (
        "the overlay removes the container but leaves the core looking for it — that is the "
        "`unreachable` state, not `absent` (#962)"
    )


def test_the_core_does_not_hard_depend_on_the_local_runtime() -> None:
    """Without this the whole `up` fails on a service that is absent on purpose."""
    depends = _fragment(CORE_FRAGMENT)["services"]["core-app"]["depends_on"]
    assert depends["ollama"] == {"condition": "service_started", "required": False}


def test_a_blank_ollama_url_survives_interpolation() -> None:
    """``${OLLAMA_URL-...}``, not ``${OLLAMA_URL:-...}``.

    The difference is the whole feature: ``:-`` substitutes the default back in for an
    *empty* value, and empty is exactly what an operator sets to say "there is no local
    runtime here". A one-character slip turns the mode off with no other symptom.
    """
    env = _fragment(CORE_FRAGMENT)["services"]["core-app"]["environment"]
    assert env["OLLAMA_URL"] == "${OLLAMA_URL-http://ollama:11434}"


def test_the_opt_out_is_reachable_as_one_command() -> None:
    """The overlay only helps if an operator can find it; `task hosted-only-up` is how."""
    taskfile = yaml.safe_load((REPO / "Taskfile.yml").read_text(encoding="utf-8"))
    cmds = " ".join(str(c) for c in taskfile["tasks"]["hosted-only-up"]["cmds"])
    assert "infra/ollama/compose.hosted-only.yaml" in cmds
    assert "-f compose.yaml" in cmds, "the overlay must be layered over the assembled stack"


def test_the_compose_gate_keeps_booting_the_default_stack() -> None:
    """`runtime-smoke` asserts ollama-init's exit code and a KV-cache restart — both need it.

    It boots the plain stack with no overlay, which is the point: the gate and the documented
    install are the same shape, so one cannot drift from the other unnoticed.
    """
    smoke = (REPO / "infra" / "ci" / "smoke.sh").read_text(encoding="utf-8")
    assert "compose.hosted-only.yaml" not in smoke
    assert "ollama-init" in smoke, "the gate no longer asserts the local runtime's one-shot"


def test_the_kubernetes_gate_asserts_the_hosted_only_mode() -> None:
    """The mode is hosted-only-shaped; only a boot can say the core answers instead of 500ing."""
    gate = (REPO / "infra" / "ci" / "k8s-smoke.sh").read_text(encoding="utf-8")
    assert "/platform/v1/llm/local-runtime" in gate, (
        "k8s-smoke no longer asserts the local-runtime state endpoint (#962)"
    )
    assert '"state":"absent"' in gate
    assert "ollama.external.url=" in gate, (
        "k8s-smoke never reaches the hosted-only mode — the upgrade phase is what blanks the "
        "URL, and without it the assertions above are made against a configured runtime"
    )


# ── Chart ────────────────────────────────────────────────────────────────────────

pytestmark_helm = pytest.mark.skipif(
    shutil.which("helm") is None, reason="these assertions render the chart with helm"
)


def _render(*sets: str) -> subprocess.CompletedProcess[str]:
    args = ["helm", "template", "epicurus", str(CHART)]
    for pair in sets:
        args += ["--set", pair]
    return subprocess.run(args, capture_output=True, text=True, timeout=120)


def _env_of(rendered: str, container: str = "core-app") -> dict[str, str]:
    """The core-app container's plain-value env, keyed by name."""
    for doc in yaml.safe_load_all(rendered):
        if not isinstance(doc, dict) or doc.get("kind") != "Deployment":
            continue
        if doc["metadata"]["name"] != container:
            continue
        spec = doc["spec"]["template"]["spec"]["containers"][0]
        return {e["name"]: e.get("value", "") for e in spec["env"]}
    raise AssertionError(f"no {container} Deployment in the render")


@pytestmark_helm
def test_the_chart_renders_a_deployment_with_no_local_runtime() -> None:
    """It did not, before: `ollama.enabled: false` `required`d an external URL (#962)."""
    result = _render(
        "ollama.enabled=false",
        "core.llm.defaultModel=claude/claude-sonnet-4-6",
        "core.memoryEmbedModel=gpt/text-embedding-3-small",
    )
    assert result.returncode == 0, result.stderr
    env = _env_of(result.stdout)
    assert env["OLLAMA_URL"] == ""
    # Nothing to pull into, so the chart's own default stops asking the core to try.
    assert env["LLM_BOOTSTRAP_MODELS"] == ""
    assert "app.kubernetes.io/component: ollama" not in result.stdout


@pytestmark_helm
def test_an_explicit_bootstrap_list_is_left_alone() -> None:
    """Only the chart's own `auto` is rewritten — an operator's list is a stated pin."""
    result = _render(
        "ollama.enabled=false",
        "core.llm.defaultModel=claude/claude-sonnet-4-6",
        "core.memoryEmbedModel=gpt/text-embedding-3-small",
        "core.llm.bootstrapModels=llama3.2",
    )
    assert result.returncode == 0, result.stderr
    assert _env_of(result.stdout)["LLM_BOOTSTRAP_MODELS"] == "llama3.2"


@pytestmark_helm
def test_an_external_runtime_still_renders_its_url() -> None:
    result = _render("ollama.enabled=false", "ollama.external.url=http://gpu-box:11434")
    assert result.returncode == 0, result.stderr
    assert _env_of(result.stdout)["OLLAMA_URL"] == "http://gpu-box:11434"


@pytestmark_helm
def test_the_default_release_is_unchanged() -> None:
    result = _render()
    assert result.returncode == 0, result.stderr
    env = _env_of(result.stdout)
    assert env["OLLAMA_URL"] == "http://ollama:11434"
    assert env["LLM_BOOTSTRAP_MODELS"] == "auto"


@pytestmark_helm
@pytest.mark.parametrize("key", ["core.memoryEmbedModel", "core.llm.defaultModel"])
def test_a_hosted_only_release_with_a_local_model_name_is_refused(key: str) -> None:
    """The guard that replaces the old one — and the reason the old one was wrong.

    `required: ollama.external.url` refused a *legitimate* deployment. This refuses a broken
    one: with no local runtime, a bare model name routes to a runtime that does not exist, so
    chat works through the hosted provider while memory recall and every module index fail at
    call time. That is the half-working stack we refuse to ship elsewhere.
    """
    hosted = {
        "core.memoryEmbedModel": "gpt/text-embedding-3-small",
        "core.llm.defaultModel": "claude/claude-sonnet-4-6",
    }
    sets = ["ollama.enabled=false"]
    sets += [f"{k}={v}" for k, v in hosted.items() if k != key]
    sets.append(f"{key}=a-local-model-name")

    result = _render(*sets)
    assert result.returncode != 0, "the chart rendered a half-working hosted-only release"
    assert "no local LLM runtime" in result.stderr
    assert key in result.stderr, "the refusal must name the key the operator has to change"
    assert "gpt/text-embedding-3-small" in result.stderr, "and an example of what to set it to"


@pytestmark_helm
def test_an_explicitly_local_prefixed_model_is_refused_too() -> None:
    """`local/llama3.2` is as unrunnable here as `llama3.2` — mirrors `providers.is_hosted`."""
    result = _render(
        "ollama.enabled=false",
        "core.llm.defaultModel=local/llama3.2",
        "core.memoryEmbedModel=gpt/text-embedding-3-small",
    )
    assert result.returncode != 0
    assert "no local LLM runtime" in result.stderr


@pytestmark_helm
def test_the_guard_does_not_fire_when_a_runtime_exists() -> None:
    """Bare names are the *right* answer with a runtime — the chart's own defaults are bare."""
    assert _render().returncode == 0
    external = _render("ollama.enabled=false", "ollama.external.url=http://gpu-box:11434")
    assert external.returncode == 0
