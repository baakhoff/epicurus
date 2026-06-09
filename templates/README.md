# templates

Scaffolding templates.

## service-template

A [cookiecutter](https://cookiecutter.readthedocs.io) that generates a new module
with the full contract pre-wired: an `EpicurusModule` with a sample tool, a
runnable ASGI app (ops `/health` + `/metrics` and the MCP tools over HTTP), a
test, a `Dockerfile`, and a per-module compose fragment.

### Generate a module

```bash
uv run cookiecutter templates/service-template -o services
# prompts for the service name etc. -> creates services/<slug>/
```

### After generating

1. `uv sync --all-packages` — install the new module into the workspace.
2. Add its fragment to the top-level `compose.yaml` `include:` list.
3. Register its package with mypy: add `-p epicurus_<slug>` to the **Mypy** step in
   `.github/workflows/ci.yml` and the `type` task in `Taskfile.yml`.
4. `uv run pytest services/<slug>` — confirm it's green.

The goal: a new "block" starts as a one-command scaffold, not a blank directory.
See the module READMEs and [Building a module](../docs/developer/building-a-module.md).
