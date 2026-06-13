# {{ cookiecutter.service_name }}

{{ cookiecutter.description }}

An epicurus module built on `epicurus-core`: it exposes MCP tools to the agent and
can publish/consume NATS events. Start from the sample `ping` tool in
`src/{{ cookiecutter.package_name }}/service.py` and replace it with the real
capability.

## Develop

```bash
uv sync --all-packages
uv run pytest services/{{ cookiecutter.service_slug }}
```

Register the package in the root `pyproject.toml` so the gates cover it:
add `{{ cookiecutter.package_name }}` to `[tool.mypy] packages` and to
`[tool.ruff.lint.isort] known-first-party`.

## Wire it into the stack

A module is invisible to the agent until the core is told about it. Do **all** of
these — the runtime smoke gate (`infra/ci/smoke.sh`, run in CI) boots the stack and
fails the build if a module is present but not discoverable, so a skipped step is
caught here, not after merge:

1. **Add the fragment** to the top-level `compose.yaml` `include:` list:
   `- services/{{ cookiecutter.service_slug }}/compose.yaml`. The smoke gate
   derives the module set from this list, so once it's here your module is gated.
2. **Tell the core about it** — add `http://{{ cookiecutter.service_slug }}:8080`
   to `module_urls` in `services/core-app/src/epicurus_core_app/settings.py`.
   This is the single most-forgotten step: without it the agent never discovers
   the module, and the smoke gate's discovery check fails.
3. **Use a unique host port** — `{{ cookiecutter.port }}` is the default; make
   sure no other fragment already publishes it (the gate flags duplicates).

```bash
docker compose up -d {{ cookiecutter.service_slug }}
```

### If the module calls back into the core

For LLM/embedding access, add `PLATFORM_URL: http://core-app:8080` to this
fragment's `environment:` and use `epicurus_core`'s `PlatformClient` — modules
never hold model keys (ADR-0010). For per-tenant secrets, register a base path
under the `epicurus-core` OpenBao policy (see
[docs/infrastructure/secrets.md](../../docs/infrastructure/secrets.md)); the core
proxies secret access so the module never talks to OpenBao directly.

See the [Building a module](../../docs/developer/building-a-module.md) guide for
the full contract (tools, events, manifest, secrets, tenant scoping).
