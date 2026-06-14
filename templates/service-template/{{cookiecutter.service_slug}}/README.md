# {{ cookiecutter.service_name }}

{{ cookiecutter.description }}

An epicurus module built on `epicurus-core`: it exposes MCP tools to the agent and
can publish/consume NATS events. Start from the sample `ping` tool in
`src/{{ cookiecutter.package_name }}/service.py` and replace it with the real
capability — `service.py` also carries copy-ready reference patterns for OAuth
tokens and large-integer columns.

## Already wired in

`task new-module` scaffolded this module **and** wired it into the stack, so there
are no manual edits to make before it runs (the runtime smoke gate would fail the
build if any of these were missing):

- registered in the root `pyproject.toml` — `[tool.mypy] packages` and
  `[tool.ruff.lint.isort] known-first-party`;
- included in the top-level `compose.yaml` `include:` list;
- registered with the core as `http://{{ cookiecutter.service_slug }}:8080` in
  `module_urls` (`services/core-app/src/epicurus_core_app/settings.py`);
- assigned the unique host port `{{ cookiecutter.port }}` — see the registry in
  [docs/reference/ports.md](../../docs/reference/ports.md); override with the
  `{{ cookiecutter.port_env }}` env var;
- reset in the smoke CI override (`infra/ci/compose.ci.yaml`) so it leaks no host
  port when the smoke stack runs beside your dev stack.

> Scaffolded with a bare `cookiecutter` call instead of `task new-module`? Then do
> those steps by hand — `task smoke` boots the stack and fails if any is missing.
> The [Building a module](../../docs/developer/building-a-module.md) guide lists them.

## Develop

```bash
uv sync --all-packages
uv run pytest services/{{ cookiecutter.service_slug }}
docker compose up -d {{ cookiecutter.service_slug }}
```

### If the module calls back into the core

For LLM/embedding access or connected-account (OAuth) tokens, uncomment
`PLATFORM_URL` in this fragment's `environment:` and use `epicurus_core`'s
`PlatformClient` — modules never hold model keys or client secrets (ADR-0010,
ADR-0016). Fetch OAuth tokens only through `PlatformClient.get_oauth_token`. For
per-tenant secrets, register a base path under the `epicurus-core` OpenBao policy
(see [docs/infrastructure/secrets.md](../../docs/infrastructure/secrets.md)); the
core proxies secret access so the module never talks to OpenBao directly.

See the [Building a module](../../docs/developer/building-a-module.md) guide for
the full contract (tools, events, manifest, secrets, tenant scoping).
