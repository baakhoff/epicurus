# templates

Scaffolding templates.

## service-template

A [cookiecutter](https://cookiecutter.readthedocs.io) that generates a new module
with the full contract pre-wired: an `EpicurusModule` with a sample tool, a
runnable ASGI app (ops `/health` + `/metrics` and the MCP tools over HTTP), a
test, a `Dockerfile`, and a per-module compose fragment. `service.py` also carries
copy-ready reference patterns for OAuth tokens and large-integer columns.

### Generate a module (recommended)

```bash
task new-module -- "My Module"
# or: uv run python scripts/new_module.py "My Module"
```

`scripts/new_module.py` renders the template **and** performs every wire-in step
the runtime smoke gate enforces, so the new module passes `task smoke` with no
manual edits:

- picks the next free host port from [docs/reference/ports.md](../docs/reference/ports.md)
  (`--port` to choose one; it refuses a port already in use);
- registers the package in the root `pyproject.toml` (`[tool.mypy] packages` +
  `[tool.ruff.lint.isort] known-first-party`);
- adds the fragment to the top-level `compose.yaml` `include:` list;
- registers `http://<slug>:8080` in the core's `module_urls`;
- resets its host port in the smoke override (`infra/ci/compose.ci.yaml`);
- refreshes `uv.lock` so the Docker build sees the new workspace member.

Then:

```bash
uv sync --all-packages
uv run pytest services/<slug>
task smoke   # boot the stack and assert the integration last mile
```

### Generate with bare cookiecutter

```bash
uv run cookiecutter templates/service-template -o services
```

This renders the files but does **not** wire the module in — do the steps above by
hand, or the smoke gate fails on first boot. Prefer `task new-module`.

The goal: a new "block" is a one-command, collision-proof, self-wiring scaffold —
not a blank directory. See the module READMEs and
[Building a module](../docs/developer/building-a-module.md).
