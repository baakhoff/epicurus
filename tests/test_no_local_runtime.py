"""A deployment may run no local LLM runtime at all — on either runtime (#962, ADR-0144).

Three states, not two: the stack's own Ollama, an external one, and *none*. The third was
unreachable on both runtimes — the Helm chart `required`d an external URL the moment Ollama
was disabled, and Compose included the fragment unconditionally with `core-app` hard-depending
on the service. These are the static and render-time halves of the fix:

* **Compose** — parsed from the fragments directly, in the style of ``test_compose_ports.py``:
  the profile that lets Ollama be left out, the `required: false` that stops its absence
  failing the whole `up`, and the env interpolation that carries a *deliberately blank*
  ``OLLAMA_URL`` through to the core. Plus the thing that is easy to get wrong and impossible
  to see: that turning the runtime into a profile did not turn local AI off for everyone who
  was not asking for that.
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

LOCAL_AI_PROFILE = "local-ai"

# Every command that starts the stack must select the profile, or the operator who typed it
# silently loses local AI. The Taskfile tasks are listed by name so a *new* start task that
# forgets it is a conversation at review, not a surprise on someone's box.
_START_TASKS = ("up", "obs-up", "docker-socket-up", "external-mounts-up")


def _fragment(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


# ── Compose ──────────────────────────────────────────────────────────────────────


def test_the_local_runtime_can_be_left_out_of_the_stack() -> None:
    services = _fragment(OLLAMA_FRAGMENT)["services"]
    for name in ("ollama", "ollama-init"):
        assert services[name].get("profiles") == [LOCAL_AI_PROFILE], (
            f"{name} must carry the {LOCAL_AI_PROFILE!r} profile — it is how a hosted-only "
            "deployment leaves the local runtime out (#962)"
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


def test_every_start_path_still_turns_local_ai_on() -> None:
    """A profile is opt-in by construction; the default install must not have changed.

    This is the regression guard for the way this feature could hurt people who did not ask
    for it: put a profile on Ollama and every existing `task up`, every deploy reconcile and
    every documented `.env` stops starting the local runtime. So each of those paths selects
    the profile explicitly, and this test is what keeps it that way.
    """
    taskfile = yaml.safe_load((REPO / "Taskfile.yml").read_text(encoding="utf-8"))
    for name in _START_TASKS:
        cmds = " ".join(str(c) for c in taskfile["tasks"][name]["cmds"])
        assert f"--profile {LOCAL_AI_PROFILE}" in cmds, (
            f"task {name} no longer starts the local AI runtime — a profile is opt-in, so "
            "every start path has to select it or the default install silently changes"
        )

    reconcile = (REPO / "infra" / "cd" / "reconcile.sh").read_text(encoding="utf-8")
    assert f"--profile {LOCAL_AI_PROFILE}" in reconcile, (
        "infra/cd/reconcile.sh would take the local runtime down on the next deploy of a "
        "box that never asked for a hosted-only stack"
    )
    assert "EPICURUS_LOCAL_AI" in reconcile, "the deploy path has no way to opt out"

    env_example = (REPO / ".env.example").read_text(encoding="utf-8")
    assert f"COMPOSE_PROFILES={LOCAL_AI_PROFILE}" in env_example, (
        ".env.example must ship the profile: it is what selects local AI for a bare "
        "`docker compose up -d`"
    )


def test_the_compose_smoke_gate_boots_the_local_runtime() -> None:
    """`runtime-smoke` asserts ollama-init's exit code and a KV-cache restart — both need it."""
    smoke = (REPO / "infra" / "ci" / "smoke.sh").read_text(encoding="utf-8")
    assert f"--profile {LOCAL_AI_PROFILE}" in smoke


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
