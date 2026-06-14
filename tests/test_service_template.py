"""The service-template renders a valid, Jinja-free module skeleton."""

from __future__ import annotations

from pathlib import Path

import pytest
from cookiecutter.exceptions import FailedHookException
from cookiecutter.main import cookiecutter

TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "service-template"


def test_template_renders(tmp_path: Path) -> None:
    out = Path(
        cookiecutter(
            str(TEMPLATE),
            no_input=True,
            output_dir=str(tmp_path),
            extra_context={"service_name": "Demo Thing"},
        )
    )
    assert out.name == "demo-thing"

    pkg = out / "src" / "epicurus_demo_thing"
    for rel in ("pyproject.toml", "Dockerfile", "compose.yaml", "README.md"):
        assert (out / rel).is_file(), rel
    for rel in ("__init__.py", "service.py", "app.py", "__main__.py", "py.typed"):
        assert (pkg / rel).is_file(), rel

    # Rendered files carry no leftover Jinja, and the Python is valid.
    service_src = (pkg / "service.py").read_text(encoding="utf-8")
    assert "cookiecutter" not in service_src
    assert 'MODULE_NAME = "demo-thing"' in service_src
    for name in ("service.py", "app.py", "__main__.py"):
        compile((pkg / name).read_text(encoding="utf-8"), name, "exec")

    # The compose fragment publishes its host port via the `<SLUG>_PORT` env var
    # (the convention every shipped module follows), defaulting to the free 8092.
    compose = (out / "compose.yaml").read_text(encoding="utf-8")
    assert "${BIND_ADDRESS:-127.0.0.1}:${DEMO_THING_PORT:-8092}:8080" in compose

    # The binding reference patterns ship in the scaffold (issue #113 decisions 3, 4).
    assert "get_oauth_token" in service_src
    assert "BigInteger" in service_src

    # The app must serve GET /manifest, or the core can't discover the module and
    # the smoke gate's discovery check fails (ADR-0004).
    app_src = (pkg / "app.py").read_text(encoding="utf-8")
    assert "add_manifest_route(app, module)" in app_src


def test_template_rejects_a_reserved_slug(tmp_path: Path) -> None:
    # The pre_gen hook aborts before writing files when the name collides with a
    # core/infra service (cookiecutter wraps the hook's non-zero exit).
    with pytest.raises(FailedHookException):
        cookiecutter(
            str(TEMPLATE),
            no_input=True,
            output_dir=str(tmp_path),
            extra_context={"service_name": "Postgres"},
        )
