"""Web search through OpenRouter is configured the same way on every platform (#984).

The core reads two keys for it — ``OPENROUTER_WEB_SEARCH_MODEL`` and
``OPENROUTER_WEB_SEARCH_ENGINE`` — and both deployments must hand them over with the core's own
defaults, so an operator who says nothing gets the same search on Compose and on Kubernetes:

* **Compose** — parsed from the core-app fragment: each key passed through from ``.env`` as
  ``${KEY:-<core default>}``, and ``.env.example`` documents both.
* **Chart** — the values carry the same defaults and the template wires them to the same env
  names (read statically, always); real ``helm template`` renders then check the env the core
  container actually gets, skipped (never silently passed) without ``helm``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from epicurus_core_app.settings import CoreAppSettings

REPO = Path(__file__).resolve().parents[1]
CHART = REPO / "infra" / "k8s" / "epicurus"
CORE_FRAGMENT = REPO / "services" / "core-app" / "compose.yaml"
ENV_EXAMPLE = REPO / ".env.example"
_TEMPLATE = "templates/core-app.yaml"

_DEFAULTS = CoreAppSettings.model_fields
# env name -> (settings field, chart values key under core.openrouterWebSearch)
_KEYS = {
    "OPENROUTER_WEB_SEARCH_MODEL": ("openrouter_web_search_model", "model"),
    "OPENROUTER_WEB_SEARCH_ENGINE": ("openrouter_web_search_engine", "engine"),
}


def _core_default(env: str) -> str:
    return str(_DEFAULTS[_KEYS[env][0]].default)


# ── Compose ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("env", sorted(_KEYS))
def test_compose_passes_the_key_through_with_the_cores_default(env: str) -> None:
    loaded = yaml.safe_load(CORE_FRAGMENT.read_text(encoding="utf-8"))
    environment = loaded["services"]["core-app"]["environment"]
    assert environment[env] == f"${{{env}:-{_core_default(env)}}}"


@pytest.mark.parametrize("env", sorted(_KEYS))
def test_env_example_documents_the_key_with_the_cores_default(env: str) -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert re.search(rf"^# {env}={re.escape(_core_default(env))}\s", text, re.MULTILINE)


# ── Chart (static) ───────────────────────────────────────────────────────────────


def _values() -> dict[str, Any]:
    loaded = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


@pytest.mark.parametrize("env", sorted(_KEYS))
def test_chart_values_carry_the_cores_default(env: str) -> None:
    assert _values()["core"]["openrouterWebSearch"][_KEYS[env][1]] == _core_default(env)


@pytest.mark.parametrize("env", sorted(_KEYS))
def test_chart_template_wires_the_value_to_the_env_name(env: str) -> None:
    template = (CHART / "templates" / "core-app.yaml").read_text(encoding="utf-8")
    key = _KEYS[env][1]
    pattern = (
        rf"- name: {env}\n\s+value: \{{\{{ \.Values\.core\.openrouterWebSearch\.{key}"
        r" \| default \"\" \| quote \}\}"
    )
    assert re.search(pattern, template), f"core-app.yaml does not render {env}"


# ── Chart (rendered) ─────────────────────────────────────────────────────────────

pytestmark_helm = pytest.mark.skipif(
    shutil.which("helm") is None, reason="these assertions render the chart with helm"
)


def _core_env(*sets: str) -> dict[str, str]:
    args: list[str] = []
    for pair in sets:
        args += ["--set", pair]
    result = subprocess.run(
        ["helm", "template", "epicurus", str(CHART), "--show-only", _TEMPLATE, *args],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    for doc in yaml.safe_load_all(result.stdout):
        if isinstance(doc, dict) and doc.get("kind") == "Deployment":
            container = doc["spec"]["template"]["spec"]["containers"][0]
            return {e["name"]: e.get("value", "") for e in container["env"]}
    raise AssertionError("no core-app Deployment in the render")


@pytestmark_helm
def test_a_default_release_renders_the_cores_defaults() -> None:
    env = _core_env()
    for name in _KEYS:
        assert env[name] == _core_default(name)


@pytestmark_helm
def test_the_operator_can_override_both() -> None:
    env = _core_env(
        "core.openrouterWebSearch.model=google/gemini-2.5-flash-lite",
        "core.openrouterWebSearch.engine=auto",
    )
    assert env["OPENROUTER_WEB_SEARCH_MODEL"] == "google/gemini-2.5-flash-lite"
    assert env["OPENROUTER_WEB_SEARCH_ENGINE"] == "auto"


@pytestmark_helm
def test_a_blank_value_renders_blank_which_the_core_reads_as_its_default() -> None:
    env = _core_env("core.openrouterWebSearch.model=")
    assert env["OPENROUTER_WEB_SEARCH_MODEL"] == ""
    # …and blank is the default, not an empty model id sent to OpenRouter.
    assert CoreAppSettings(
        openrouter_web_search_model=""
    ).openrouter_web_search_model == _core_default("OPENROUTER_WEB_SEARCH_MODEL")
