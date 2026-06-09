"""The service-template renders a valid, Jinja-free module skeleton."""

from __future__ import annotations

from pathlib import Path

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
