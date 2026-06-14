"""``scripts/new_module.py`` scaffolds AND wires a module in — zero manual edits (#113).

These exercise the orchestrator against a throwaway copy of the repo so the real
tree is never touched (``sync=False`` skips the ``uv.lock`` refresh).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[1]

# Enough of the real stack copied in that the port scan and the wiring targets
# are realistic (core-app holds the module_urls; the rest bind ports).
_SERVICE_FRAGMENTS = ("core-app", "mail", "tasks")


def _make_repo(new_module: ModuleType, dest: Path) -> Path:
    shutil.copytree(REPO / "templates", dest / "templates")
    shutil.copy(REPO / "pyproject.toml", dest / "pyproject.toml")
    shutil.copy(REPO / "compose.yaml", dest / "compose.yaml")
    for svc in _SERVICE_FRAGMENTS:
        target = dest / "services" / svc / "compose.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / "services" / svc / "compose.yaml", target)
    settings_dst = dest / new_module.SETTINGS_REL
    settings_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / new_module.SETTINGS_REL, settings_dst)
    edge_dst = dest / "infra" / "edge" / "compose.yaml"
    edge_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / "infra" / "edge" / "compose.yaml", edge_dst)
    ci_dst = dest / new_module.CI_OVERRIDE_REL
    ci_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / new_module.CI_OVERRIDE_REL, ci_dst)
    return dest


def test_name_derivation_matches_template(new_module: ModuleType) -> None:
    assert new_module.slugify("My Module") == "my-module"
    assert new_module.slugify("Weather_Bot") == "weather-bot"
    assert new_module.package_name("my-module") == "epicurus_my_module"


def test_next_free_port_in_repo(new_module: ModuleType) -> None:
    # 8082-8092 are taken in the real tree (notes took 8092); next free is 8093.
    assert new_module.next_free_port(REPO) == 8093
    assert 8093 not in new_module.published_ports(REPO)


def test_run_scaffolds_and_wires(new_module: ModuleType, tmp_path: Path) -> None:
    root = _make_repo(new_module, tmp_path)
    before = set(new_module.published_ports(root))

    result = new_module.run(root, "Throwaway Thing", sync=False)

    assert result.slug == "throwaway-thing"
    assert result.package == "epicurus_throwaway_thing"
    assert result.port in new_module.PORT_BAND
    assert result.port not in before

    service = root / "services" / "throwaway-thing"
    assert (service / "src" / "epicurus_throwaway_thing" / "service.py").is_file()
    compose = (service / "compose.yaml").read_text(encoding="utf-8")
    assert f"${{THROWAWAY_THING_PORT:-{result.port}}}" in compose

    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert pyproject.count('"epicurus_throwaway_thing"') == 2  # mypy packages + ruff isort

    root_compose = (root / "compose.yaml").read_text(encoding="utf-8")
    assert "- services/throwaway-thing/compose.yaml" in root_compose

    settings = (root / new_module.SETTINGS_REL).read_text(encoding="utf-8")
    assert "http://throwaway-thing:8080" in settings
    # module_urls is re-emitted one URL per line, so it never overflows the limit.
    assert all(len(line) <= 100 for line in settings.splitlines())

    # The smoke CI override resets the new module's host port (so it never leaks).
    ci_override = (root / new_module.CI_OVERRIDE_REL).read_text(encoding="utf-8")
    assert "  throwaway-thing:\n    ports: !reset []" in ci_override

    # The wired-in module must not introduce a port collision.
    bindings = new_module.port_bindings(root)
    ports = [p for p, _ in bindings]
    assert len(ports) == len(set(ports)), f"collision after scaffolding: {bindings}"


def test_wiring_is_idempotent(new_module: ModuleType, tmp_path: Path) -> None:
    root = _make_repo(new_module, tmp_path)
    new_module.run(root, "Throwaway Thing", sync=False)

    settings = root / new_module.SETTINGS_REL
    pyproject = root / "pyproject.toml"
    compose = root / "compose.yaml"
    ci_override = root / new_module.CI_OVERRIDE_REL

    def snapshot() -> tuple[str, str, str, str]:
        return (
            settings.read_text(),
            pyproject.read_text(),
            compose.read_text(),
            ci_override.read_text(),
        )

    before = snapshot()
    new_module.wire_module_urls(root, "throwaway-thing")
    new_module.wire_pyproject(root, "epicurus_throwaway_thing")
    new_module.wire_compose_include(root, "throwaway-thing")
    new_module.wire_ci_port_reset(root, "throwaway-thing")

    assert snapshot() == before


def test_rejects_taken_port(new_module: ModuleType, tmp_path: Path) -> None:
    root = _make_repo(new_module, tmp_path)
    with pytest.raises(SystemExit):
        new_module.run(root, "Dup", port=8082, sync=False)  # core-app already binds 8082


def test_rejects_existing_service(new_module: ModuleType, tmp_path: Path) -> None:
    root = _make_repo(new_module, tmp_path)
    new_module.run(root, "Throwaway Thing", sync=False)
    with pytest.raises(SystemExit):
        new_module.run(root, "Throwaway Thing", sync=False)


def test_rejects_invalid_name(new_module: ModuleType, tmp_path: Path) -> None:
    root = _make_repo(new_module, tmp_path)
    with pytest.raises(SystemExit):
        new_module.run(root, "123 Bad!!", sync=False)
